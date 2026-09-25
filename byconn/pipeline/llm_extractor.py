"""Real LLM-backed structured extraction for the BYCONN-X pipeline.

:class:`LLMExtractor` extends the original :class:`~byconn.pipeline.entity_extractor.EntityExtractor`
prompt contract with an actual provider call and normalises whatever comes back
into a predictable shape. Provider credentials and model selection come from
the environment, and the extractor degrades to an empty-but-valid result when no
key is configured so that crawling never dies because the LLM is unavailable.

**Open-source and free-tier support.** Every OpenAI-compatible endpoint is
supported through one code path: hosted OpenAI, a local Ollama server, Groq's
free tier, vLLM, LM Studio, or anything else speaking ``/v1/chat/completions``.
Selection is driven entirely by the environment::

    OPENAI_API_KEY=ollama
    OPENAI_API_BASE=http://localhost:11434/v1
    MODEL_NAME=llama3.2
    VISION_MODEL_NAME=llava

``OPENAI_API_KEY=ollama`` implies the local default base URL, so a local model
needs no ``OPENAI_API_BASE`` at all. Set ``GROQ_API_KEY`` for Groq's free tier.

Open-source models are far less consistent than hosted ones about JSON mode, so
responses are parsed defensively (fenced blocks, surrounding prose, single
quotes, trailing commas, unbalanced braces) and a JSON-mode failure is
retried once as a plain prompt.
"""

import ast
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

# Defaults per provider. The openai branch covers every OpenAI-compatible
# endpoint; local servers get a small open-weights model, hosted gets a cheap
# hosted one. Both are overridable via MODEL_NAME.
DEFAULT_MODEL = {
    "openai": "gpt-4o-mini",
    "local": "llama3.2",
    "gemini": "gemini-3.8-flash",
}
DEFAULT_VISION_MODEL = {
    "openai": "gpt-4o-mini",
    "local": "llava",
    "gemini": "gemini-3.8-flash",
}

# Well-known OpenAI-compatible services. "key" is a literal for endpoints that
# ignore credentials; "key_env" names the variable to read.
COMPATIBLE_ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "key": "ollama",
        "key_env": "OPENAI_API_KEY",
        "model": "llama3.2",
        "vision_model": "llava",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
        "vision_model": "meta-llama/llama-4-scout-17b-16e-instruct",
    },
    "lmstudio": {"base_url": "http://localhost:1234/v1", "key": "lm-studio"},
}

# Hostnames that indicate a locally hosted model server.
LOCAL_HOST_PATTERN = re.compile(
    r"^(?:[a-z][a-z0-9+.\-]*://)?"
    r"(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|host\.docker\.internal)"
    r"(?::\d+)?(?:/|$)",
    re.I,
)
# The conventional placeholder key that flags a local Ollama server.
LOCAL_KEY_MARKERS = {"ollama", "local", "none", "no-key", "nokey"}

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
        vision_model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
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
        self._base_url_override = base_url
        self.temperature = temperature if temperature is not None else _env_float("LLM_TEMPERATURE", 0.0)
        self.max_input_tokens = max_input_tokens or _env_int("LLM_MAX_INPUT_TOKENS", DEFAULT_MAX_INPUT_TOKENS)
        self.max_retries = max_retries if max_retries is not None else _env_int("LLM_MAX_RETRIES", 2)
        self.max_entities = max_entities
        self.max_triples = max_triples
        # Resolved before the models so _model_kind() can inspect the endpoint.
        self.base_url = self._resolve_base_url()
        self.model = model or self._resolve_model()
        self.vision_model = vision_model or self._resolve_vision_model()
        self._client = None

    # --- provider wiring ----------------------------------------------------
    @staticmethod
    def _detect_provider() -> str:
        """Infers the provider from whichever credential is configured.

        A local OpenAI-compatible server is treated as the ``openai`` provider
        with a local flavour, so one code path serves hosted and local models.
        """
        if os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_BASE"):
            return "openai"
        if os.getenv("GROQ_API_KEY"):
            return "groq"
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return "gemini"
        return "openai"

    @staticmethod
    def _is_local_endpoint(base_url: Optional[str], key: Optional[str]) -> bool:
        """Detects a locally hosted model server.

        True when the base URL points at loopback, or when the credential is
        one of the conventional local placeholders (``ollama``, ``local``...).
        """
        if base_url and LOCAL_HOST_PATTERN.match(base_url):
            return True
        return bool(key) and key.strip().lower() in LOCAL_KEY_MARKERS

    def _resolve_base_url(self) -> Optional[str]:
        """Resolves the base URL for OpenAI-compatible providers."""
        if self._base_url_override:
            return self._base_url_override.rstrip("/")
        explicit = os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")
        if explicit:
            return explicit.rstrip("/")

        key = os.getenv("OPENAI_API_KEY")
        if key and key.strip().lower() == "ollama":
            # The documented local default; no OPENAI_API_BASE required.
            return COMPATIBLE_ENDPOINTS["ollama"]["base_url"]
        if key and key.strip().lower() == "lm-studio":
            return COMPATIBLE_ENDPOINTS["lmstudio"]["base_url"]
        if self.provider == "groq" or os.getenv("GROQ_API_KEY"):
            return COMPATIBLE_ENDPOINTS["groq"]["base_url"]
        return None

    def _resolve_api_key(self) -> Optional[str]:
        """Returns the credential for the active provider, or None if unset."""
        if self.api_key:
            return self.api_key
        if self.provider == "gemini":
            return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if self.provider == "groq":
            return os.getenv("GROQ_API_KEY")
        key = os.getenv("OPENAI_API_KEY")
        if key:
            return key
        # A bare local endpoint still needs a placeholder credential.
        if self.base_url and self._is_local_endpoint(self.base_url, None):
            return COMPATIBLE_ENDPOINTS["ollama"]["key"]
        return None

    def _model_kind(self) -> str:
        """Returns the flavour used to pick defaults: hosted, local or gemini."""
        if self.provider == "gemini":
            return "gemini"
        if self._is_local_endpoint(self.base_url, os.getenv("OPENAI_API_KEY")):
            return "local"
        return "openai"

    def _resolve_model(self) -> str:
        """Picks the text model id.

        Precedence: ``MODEL_NAME`` (the open-weights convention) then the
        legacy per-provider ``*_MODEL`` variable, then a sensible default for
        the endpoint flavour.
        """
        if self.provider == "gemini":
            return os.getenv("GEMINI_MODEL") or DEFAULT_MODEL["gemini"]
        if self.provider == "groq":
            return (
                os.getenv("MODEL_NAME")
                or COMPATIBLE_ENDPOINTS["groq"]["model"]
            )
        kind = self._model_kind()
        return (
            os.getenv("MODEL_NAME")
            or os.getenv("OPENAI_MODEL")
            or DEFAULT_MODEL[kind]
        )

    def _resolve_vision_model(self) -> str:
        """Picks the vision model id used for screenshot-driven decisions.

        Precedence: ``VISION_MODEL_NAME`` then ``MODEL_NAME`` (when the chosen
        model is a known vision-capable one) then a per-flavour default.
        """
        explicit = os.getenv("VISION_MODEL_NAME")
        if explicit:
            return explicit
        if self.provider == "gemini":
            return os.getenv("GEMINI_MODEL") or DEFAULT_VISION_MODEL["gemini"]
        if self.provider == "groq":
            return COMPATIBLE_ENDPOINTS["groq"]["vision_model"]
        return DEFAULT_VISION_MODEL[self._model_kind()]

    @property
    def is_available(self) -> bool:
        """Reports whether a credential or endpoint is present."""
        return bool(self._resolve_api_key())

    @property
    def supports_json_mode(self) -> bool:
        """Whether the endpoint is expected to honour ``response_format``.

        Local open-weights models frequently ignore or reject JSON mode, so it
        is skipped for loopback endpoints and retried without it on refusal.
        """
        if self.provider == "gemini":
            return True
        return not self._is_local_endpoint(self.base_url, os.getenv("OPENAI_API_KEY"))

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

            # base_url routes the SDK to Ollama / Groq / vLLM / LM Studio.
            self._client = AsyncOpenAI(
                api_key=key,
                base_url=self.base_url,
                max_retries=0,  # retries are handled by _call_with_retries
            )
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
        self, prompt: str, image: Optional[bytes] = None, model: Optional[str] = None
    ) -> Optional[str]:
        """Invokes the provider with exponential backoff, returning raw text.

        Args:
            prompt: Text instruction.
            image: Optional image bytes (PNG/JPEG) for vision-capable models.
                Ignored by models or providers that do not accept images.
            model: Optional model override, used to route vision requests to
                ``VISION_MODEL_NAME`` when it differs from the text model.
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                if self.provider == "gemini":
                    return await self._call_gemini(prompt, image, model)
                return await self._call_openai(prompt, image, model)
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

    @staticmethod
    def _json_mode_unsupported(exc: BaseException) -> bool:
        """Detects a refusal of ``response_format=json_object``.

        Local open-weights models (llava, older Gemma, Mistral builds) often
        reject the parameter outright. That is recoverable, so it must not be
        treated as a hard failure.
        """
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if status not in (400, 404, 422, None):
            return False
        text = f"{type(exc).__name__} {exc}".lower()
        markers = ("response_format", "json_object", "json mode", "json mode is not",
                   "not supported", "unsupported", "unrecognized")
        return any(marker in text for marker in markers)

    async def _call_openai(
        self, prompt: str, image: Optional[bytes] = None, model: Optional[str] = None
    ) -> str:
        """Calls an OpenAI-compatible chat completions endpoint.

        Works against hosted OpenAI and every OpenAI-compatible server
        (Ollama, Groq, vLLM, LM Studio) because the base URL is configured on
        the client. JSON mode is requested only when the endpoint is expected
        to honour it, and is retried as a plain prompt if refused, so
        open-source models still return parseable text.
        """
        client = self._get_client()
        target_model = model or self.model
        if image:
            content: Any = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": self._image_data_url(image)}},
            ]
        else:
            content = prompt

        messages = [
            {
                "role": "system",
                "content": "You extract structured knowledge graphs. Respond with JSON only.",
            },
            {"role": "user", "content": content},
        ]

        async def _create(use_json_mode: bool) -> str:
            kwargs: Dict[str, Any] = {
                "model": target_model,
                "temperature": self.temperature,
                "messages": messages,
            }
            if use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            response = await client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""

        want_json_mode = self.supports_json_mode
        try:
            return await _create(want_json_mode)
        except Exception as exc:
            if not want_json_mode or not self._json_mode_unsupported(exc):
                raise
            # The endpoint ignored our probe: retry once without JSON mode and
            # lean on the lenient parser instead.
            logger.warning(
                "Endpoint rejected response_format (model=%s); retrying as a plain "
                "prompt and parsing leniently: %s",
                target_model, exc,
            )
            return await _create(False)

    async def _call_gemini(
        self, prompt: str, image: Optional[bytes] = None, model: Optional[str] = None
    ) -> str:
        """Calls the Gemini generate-content API requesting a JSON response.

        When ``image`` is supplied it is attached as an inline image part, which
        is how the Gemini SDK accepts vision input.
        """
        client = self._get_client()
        if model and model != self.model:
            # A vision override needs its own GenerativeModel instance.
            client = type(client)(model)
        content: Any = prompt
        if image:
            content = [prompt, {"inline_data": {"mime_type": "image/png", "data": image}}]
        response = await client.generate_content_async(
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
    def _strip_fences(text: str) -> str:
        """Removes a surrounding markdown code fence, if present."""
        fence = re.search(r"```(?:[a-zA-Z0-9_+-]*)\s*\n?(.*?)```", text, re.DOTALL)
        if fence:
            return fence.group(1).strip()
        # Some models emit a lone opening fence and then prose.
        if text.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z0-9_+-]*\s*", "", text)
            return re.sub(r"```\s*$", "", stripped).strip()
        return text

    @staticmethod
    def _find_balanced(text: str) -> Optional[str]:
        """Extracts the first balanced ``{...}`` or ``[...]`` span.

        Braces inside string literals are ignored, which the previous
        ``find``/``rfind`` approach got wrong: a model returning
        ``{"note": "use {braces}"}`` was truncated mid-string.
        """
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            if start == -1:
                continue
            depth = 0
            in_string = False
            quote = ""
            escaped = False
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        in_string = False
                    continue
                if char in ("'", '"'):
                    in_string = True
                    quote = char
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        return text[start : index + 1]
        return None

    @staticmethod
    def _repair(candidate: str) -> Optional[str]:
        """Applies conservative repairs for common open-model JSON mistakes."""
        # Trailing commas before a closing brace/bracket.
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        # Unquoted object keys: {name: "x"} -> {"name": "x"}
        repaired = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_\- ]*)\s*:", r'\1"\2":', repaired)
        # Python literals: True/False/None -> true/false/null
        repaired = re.sub(r"\bTrue\b", "true", repaired)
        repaired = re.sub(r"\bFalse\b", "false", repaired)
        repaired = re.sub(r"\bNone\b", "null", repaired)
        return repaired

    @classmethod
    def _loads(cls, raw: str) -> Any:
        """Parses JSON from a model response, tolerating messy open-model output.

        Handles markdown fences, surrounding prose, single-quoted Python-style
        dicts, trailing commas and unquoted keys. Returns ``None`` only when
        nothing usable can be recovered.
        """
        text = (raw or "").strip()
        if not text:
            return None

        text = cls._strip_fences(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        candidate = cls._find_balanced(text)
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            repaired = cls._repair(candidate)
            if repaired:
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass
            # Python-style dicts with single quotes parse as Python literals.
            try:
                value = ast.literal_eval(candidate)
                if isinstance(value, (dict, list)):
                    return value
            except (ValueError, SyntaxError, TypeError):
                pass

        # Last resort: the whole (unfenced) text as a Python literal.
        try:
            value = ast.literal_eval(text)
            if isinstance(value, (dict, list)):
                return value
        except (ValueError, SyntaxError, TypeError):
            pass
        return None

    @staticmethod
    def empty_result() -> Dict[str, Any]:
        """Returns the canonical empty result, used when extraction is skipped."""
        return {"entities": [], "triples": [], "topics": [], "summary": ""}


__all__ = ["LLMExtractor"]
