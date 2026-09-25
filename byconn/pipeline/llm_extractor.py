"""Real LLM-backed structured extraction for the BYCONN-X pipeline.

:class:`LLMExtractor` extends the original :class:`~byconn.pipeline.entity_extractor.EntityExtractor`
prompt contract with an actual provider call (OpenAI or Google Gemini) and
normalises whatever comes back into a predictable shape. Provider credentials
and model selection come from the environment, and the extractor degrades to an
empty-but-valid result when no key is configured so that crawling never dies
because the LLM is unavailable.
"""

import asyncio
import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .entity_extractor import EntityExtractor

logger = logging.getLogger("byconnx.pipeline.llm_extractor")

DEFAULT_MAX_INPUT_TOKENS = 6000
DEFAULT_MODEL = {"openai": "gpt-4o-mini", "gemini": "gemini-3.8-flash"}
VALID_ENTITY_TYPES = {
    "ORGANIZATION", "PERSON", "PRODUCT", "TECHNOLOGY",
    "LOCATION", "EVENT", "CONCEPT", "ENTITY",
}


def _env_int(key: str, default: int) -> int:
    """Reads a positive int env var, falling back on absence or bad input."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using %s", key, raw, default)
        return default
    return value if value > 0 else default


def _env_float(key: str, default: float) -> float:
    """Reads a float env var, falling back on absence or bad input."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using %s", key, raw, default)
        return default


# 4xx responses other than 408/429 will not succeed on a retry.
PERMANENT_STATUS_CODES = {400, 401, 403, 404, 405, 422}
PERMANENT_ERROR_NAMES = (
    "NotFound", "BadRequest", "PermissionDenied",
    "Unauthenticated", "InvalidArgument", "UnprocessableEntity",
)


def is_permanent_error(exc: BaseException) -> bool:
    """Detects a non-retryable provider error, such as an unknown model name.

    Retrying a 404 burns the whole backoff budget on a guaranteed failure, so
    these short-circuit straight to the empty result.
    """
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status_code, int) and status_code in PERMANENT_STATUS_CODES:
        return True
    return any(marker in type(exc).__name__ for marker in PERMANENT_ERROR_NAMES)


def _coerce_list(value: Any) -> List[Any]:
    """Normalises a model field into a list, tolerating None or a scalar."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class LLMExtractor(EntityExtractor):
    """Extracts entities, relations, topics and a summary from raw page text.

    Selects a provider from ``LLM_PROVIDER`` (``openai``, ``gemini`` or
    ``auto``), falling back to whichever API key is present. Models default to
    ``OPENAI_MODEL`` / ``GEMINI_MODEL``. Every call is retried with exponential
    backoff, token-budgeted with ``tiktoken``, and its JSON response is
    validated before being returned, so callers always receive the documented
    shape.
    """

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: Optional[float] = None,
        max_input_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
        max_entities: int = 40,
        max_triples: int = 60,
    ) -> None:
        super().__init__(llm_client=None)
        # Resolve "auto" to a concrete provider once, so the provider, the API
        # key lookup and the model id can never disagree.
        requested = (provider or os.getenv("LLM_PROVIDER") or "auto").strip().lower()
        self.provider = self._detect_provider() if requested in ("", "auto") else requested
        self.api_key = api_key
        self.temperature = temperature if temperature is not None else _env_float("LLM_TEMPERATURE", 0.0)
        self.max_input_tokens = max_input_tokens or _env_int("LLM_MAX_INPUT_TOKENS", DEFAULT_MAX_INPUT_TOKENS)
        self.max_retries = max_retries if max_retries is not None else _env_int("LLM_MAX_RETRIES", 2)
        self.max_entities = max_entities
        self.max_triples = max_triples
        self.model = model or self._resolve_model()
        self._client = None

    # --- provider wiring ----------------------------------------------------
    @staticmethod
    def _detect_provider() -> str:
        """Infers the provider from whichever credential is configured."""
        if os.getenv("OPENAI_API_KEY"):
            return "openai"
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return "gemini"
        return "openai"

    def _resolve_model(self) -> str:
        """Picks the concrete model id for the active provider."""
        return (
            os.getenv(f"{self.provider.upper()}_MODEL")
            or DEFAULT_MODEL.get(self.provider, DEFAULT_MODEL["openai"])
        )

    def _resolve_api_key(self) -> Optional[str]:
        """Returns the API key for the active provider, or None if unset."""
        if self.api_key:
            return self.api_key
        if self.provider == "gemini":
            return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        return os.getenv("OPENAI_API_KEY")

    @property
    def is_available(self) -> bool:
        """Reports whether a credential is present, i.e. real calls can run."""
        return bool(self._resolve_api_key())

    def _get_client(self) -> Any:
        """Lazily builds the provider SDK client (imports are deferred)."""
        if self._client is not None:
            return self._client
        key = self._resolve_api_key()
        if not key:
            raise RuntimeError(f"No API key configured for provider '{self.provider}'")
        if self.provider == "gemini":
            import google.generativeai as genai  # deferred: optional dependency

            genai.configure(api_key=key)
            self._client = genai.GenerativeModel(self.model)
        else:
            from openai import AsyncOpenAI  # deferred: optional dependency

            self._client = AsyncOpenAI(api_key=key)
        return self._client

    # --- prompt + token budgeting -------------------------------------------
    def count_tokens(self, text: str) -> int:
        """Counts tokens with tiktoken, falling back to a word estimate."""
        try:
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(self.model)
            except Exception:
                encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text, disallowed_special=()))
        except Exception:
            return len(text.split())

    def truncate_to_budget(self, text: str) -> str:
        """Trims text to ``max_input_tokens`` so prompts stay inside limits."""
        if not text:
            return ""
        if self.count_tokens(text) <= self.max_input_tokens:
            return text
        # ~4 chars/token is a safe average for English prose; add a marker so
        # downstream summaries know the tail was dropped.
        budget_chars = self.max_input_tokens * 4
        return text[:budget_chars] + "\n\n[...truncated...]"

    # --- extraction ---------------------------------------------------------
    async def extract_knowledge(self, text_content: str) -> Dict[str, Any]:
        """Extracts a structured knowledge graph from page text.

        Returns ``{"entities": [...], "triples": [...], "topics": [...],
        "summary": "..."}``. On missing credentials or provider failure the
        same shape is returned empty rather than raising, so a crawl still
        completes and stores the page.
        """
        if not (text_content or "").strip():
            return self.empty_result()
        if not self.is_available:
            logger.warning(
                "No LLM API key configured (set OPENAI_API_KEY or GEMINI_API_KEY); "
                "skipping entity extraction."
            )
            return self.empty_result()

        prompt = self.build_extraction_prompt(self.truncate_to_budget(text_content))
        raw = await self._call_with_retries(prompt)
        if raw is None:
            return self.empty_result()
        return self.parse_response(raw)

    async def _call_with_retries(
        self, prompt: str, image: Optional[bytes] = None
    ) -> Optional[str]:
        """Invokes the provider with exponential backoff, returning raw text.

        Args:
            prompt: Text instruction.
            image: Optional image bytes (PNG/JPEG) for vision-capable models.
                Ignored by models or providers that do not accept images.
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                if self.provider == "gemini":
                    return await self._call_gemini(prompt, image)
                return await self._call_openai(prompt, image)
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries or is_permanent_error(exc):
                    if is_permanent_error(exc):
                        logger.error("Non-retryable LLM error, giving up: %s", exc)
                    break
                delay = 2.0 * (2 ** attempt)
                logger.warning(
                    "LLM extraction attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1, self.max_retries + 1, exc, delay,
                )
                await asyncio.sleep(delay)
        logger.error("LLM extraction failed after %d attempts: %s", self.max_retries + 1, last_error)
        return None

    @staticmethod
    def _image_data_url(image: bytes, mime_type: str = "image/png") -> str:
        """Encodes image bytes as a base64 data URL for vision APIs."""
        return f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"

    async def _call_openai(self, prompt: str, image: Optional[bytes] = None) -> str:
        """Calls the OpenAI chat completions API in JSON-object mode.

        When ``image`` is supplied it is attached as a vision content part;
        JSON response mode is kept so the caller still receives parseable text.
        """
        client = self._get_client()
        if image:
            content: Any = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": self._image_data_url(image)}},
            ]
        else:
            content = prompt
        response = await client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You extract structured knowledge graphs. Respond with JSON only.",
                },
                {"role": "user", "content": content},
            ],
        )
        return response.choices[0].message.content or ""

    async def _call_gemini(self, prompt: str, image: Optional[bytes] = None) -> str:
        """Calls the Gemini generate-content API requesting a JSON response.

        When ``image`` is supplied it is attached as an inline image part, which
        is how the Gemini SDK accepts vision input.
        """
        model = self._get_client()
        content: Any = prompt
        if image:
            content = [prompt, {"inline_data": {"mime_type": "image/png", "data": image}}]
        response = await model.generate_content_async(
            content,
            generation_config={
                "temperature": self.temperature,
                "response_mime_type": "application/json",
            },
        )
        return getattr(response, "text", "") or ""

    # --- response validation ------------------------------------------------
    def parse_response(self, raw: str) -> Dict[str, Any]:
        """Coerces raw model output into the documented result shape.

        Tolerates markdown fences and surrounding prose, and drops malformed
        entities/relations rather than propagating bad data to the databases.
        """
        data = self._loads(raw)
        if not isinstance(data, dict):
            logger.warning("LLM response was not a JSON object; returning empty result.")
            return self.empty_result()

        entities: List[Dict[str, str]] = []
        seen_entities = set()
        for item in _coerce_list(data.get("entities")):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name.lower() in seen_entities:
                continue
            entity_type = str(item.get("type") or "ENTITY").strip().upper()
            seen_entities.add(name.lower())
            entities.append(
                {
                    "name": name[:200],
                    "type": entity_type if entity_type in VALID_ENTITY_TYPES else "ENTITY",
                }
            )
            if len(entities) >= self.max_entities:
                break

        triples: List[Dict[str, str]] = []
        seen_triples = set()
        for item in _coerce_list(data.get("triples")):
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            predicate = str(item.get("predicate") or "").strip()
            obj = str(item.get("object") or "").strip()
            if not (subject and predicate and obj):
                continue
            signature = (subject.lower(), predicate.lower(), obj.lower())
            if signature in seen_triples:
                continue
            seen_triples.add(signature)
            triples.append(
                {
                    "subject": subject[:200],
                    "predicate": predicate[:120],
                    "object": obj[:200],
                }
            )
            if len(triples) >= self.max_triples:
                break

        topics = [str(t).strip()[:80] for t in _coerce_list(data.get("topics")) if str(t).strip()][:20]
        summary = str(data.get("summary") or "").strip()[:1000]

        if not entities and not triples:
            logger.warning("LLM returned no usable entities or relations.")

        return {"entities": entities, "triples": triples, "topics": topics, "summary": summary}

    @staticmethod
    def _loads(raw: str) -> Any:
        """Parses JSON from a model response, stripping markdown fences."""
        text = (raw or "").strip()
        if not text:
            return None
        fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Fall back to the first balanced object/array in the response.
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = text.find(opener), text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    @staticmethod
    def empty_result() -> Dict[str, Any]:
        """Returns the canonical empty result, used when extraction is skipped."""
        return {"entities": [], "triples": [], "topics": [], "summary": ""}


__all__ = ["LLMExtractor"]
