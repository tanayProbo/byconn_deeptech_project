"""Document chunking and dense embedding generation for the BYCONN-X pipeline.

Embeddings are produced by a real semantic model, never a fabricated vector.
Providers are tried in this order:

1. ``sentence-transformers`` (local, e.g. ``all-MiniLM-L6-v2`` -> 384 dims).
2. OpenAI ``text-embedding-3-small`` (requesting 384 dims so vectors match the
   Qdrant collection).

If neither is available the embedder degrades to returning an empty list and
logs an actionable error. Callers treat an empty result as "no vectors were
produced" and skip vector indexing, which is far safer than persisting
meaningless vectors that would silently poison similarity search.

Both optional dependencies are imported lazily and probed with
``importlib.util.find_spec`` so that importing this module never pulls in
PyTorch or the OpenAI SDK.
"""

import asyncio
import importlib.util
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("byconnx.pipeline.embedder")

DEFAULT_DIMENSIONS = 384
DEFAULT_LOCAL_MODEL = "all-MiniLM-L6-v2"          # natively 384-dim
DEFAULT_OPENAI_MODEL = "text-embedding-3-small"   # 1536-dim, truncated via `dimensions`
DEFAULT_BATCH_SIZE = 64


def _env_str(key: str, default: str) -> str:
    """Reads a string env var, treating blank values as unset."""
    value = os.getenv(key)
    return value if value else default


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


# Values that flag a locally hosted model server rather than a hosted credential.
LOCAL_KEY_MARKERS = {"ollama", "local", "none", "no-key", "nokey"}


def _hosted_openai_key() -> Optional[str]:
    """Returns OPENAI_API_KEY only when it is a real hosted credential.

    ``OPENAI_API_KEY=ollama`` selects the local LLM backend; it cannot be used
    against the hosted embeddings endpoint, so it is ignored here.
    """
    key = os.getenv("OPENAI_API_KEY")
    if not key or key.strip().lower() in LOCAL_KEY_MARKERS:
        return None
    return key


def _module_available(name: str) -> bool:
    """Reports whether a module is installed, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class DocumentEmbedder:
    """Chunks extracted markdown text and generates dense vector embeddings.

    Also exposes sparse term-frequency weights for keyword indexing.

    Args:
        embedding_client: Optional pre-built client. Any object exposing
            ``embeddings.create(...)`` is used as an OpenAI-compatible provider.
        chunk_size: Words per chunk.
        chunk_overlap: Words shared between consecutive chunks.
        provider: ``auto``, ``sentence-transformers`` or ``openai``. Defaults
            to ``EMBEDDING_PROVIDER`` then ``auto``.
        model: Model id override. Defaults to ``EMBEDDING_MODEL``.
        dimensions: Vector width. Must match the target Qdrant collection.
        batch_size: Chunks per provider request.
    """

    def __init__(
        self,
        embedding_client: Any = None,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        batch_size: Optional[int] = None,
    ):
        self.embedding_client = embedding_client
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.dimensions = dimensions or _env_int("EMBEDDING_DIMENSIONS", DEFAULT_DIMENSIONS)
        self.batch_size = batch_size or _env_int("EMBEDDING_BATCH_SIZE", DEFAULT_BATCH_SIZE)
        self._explicit_model = model
        self.provider = self._resolve_provider(provider)
        self.model = self._resolve_model()
        self._local_model: Any = None
        self._openai_client: Any = None
        self._local_lock = asyncio.Lock()
        self._unavailable_warned = False

    # --- provider resolution ------------------------------------------------
    def _resolve_provider(self, provider: Optional[str]) -> str:
        """Determines which embedding backend to use.

        A local placeholder key (``ollama``, ``local``...) is deliberately not
        treated as a hosted OpenAI credential: this embedder targets the hosted
        embeddings API, so silently "enabling" vectors that will 401 on every
        document would be worse than reporting them as unavailable. Use
        ``EMBEDDING_PROVIDER=openai`` with a real key to override.
        """
        requested = (provider or os.getenv("EMBEDDING_PROVIDER") or "auto").strip().lower()
        if requested != "auto":
            return requested
        if self.embedding_client is not None:
            return "openai"
        if _module_available("sentence_transformers"):
            return "sentence-transformers"
        if _hosted_openai_key():
            return "openai"
        # Nothing usable is installed or configured.
        return "none"

    def _resolve_model(self) -> Optional[str]:
        """Picks the model id for the resolved provider."""
        if self._explicit_model:
            return self._explicit_model
        override = os.getenv("EMBEDDING_MODEL")
        if override:
            return override
        if self.provider == "sentence-transformers":
            return DEFAULT_LOCAL_MODEL
        if self.provider == "openai":
            return DEFAULT_OPENAI_MODEL
        return None

    @property
    def is_available(self) -> bool:
        """Reports whether real embeddings can currently be produced."""
        return self.provider in {"sentence-transformers", "openai"}

    # --- chunking -----------------------------------------------------------
    def split_into_chunks(self, text: str) -> List[str]:
        """Splits long text blocks using sliding windows to preserve context."""
        words = (text or "").split()
        if not words:
            return []
        chunks = []
        i = 0
        # A stride at or below zero would loop forever, and an overlap equal to
        # (or wider than) the window means the chunks are simply disjoint.
        step = self.chunk_size - self.chunk_overlap
        if step < 1:
            step = self.chunk_size
        while i < len(words):
            chunk_words = words[i : i + self.chunk_size]
            chunks.append(" ".join(chunk_words))
            i += step
        logger.info(f"Chunked document body into {len(chunks)} text chunks.")
        return chunks

    # --- dense embeddings ---------------------------------------------------
    async def generate_dense_embeddings(self, chunks: Sequence[str]) -> List[List[float]]:
        """Generates one dense vector per chunk using the configured provider.

        Returns an empty list (never a fabricated vector) when no provider is
        available or the provider call fails, so callers can skip indexing.
        """
        chunks = list(chunks or [])
        if not chunks:
            return []
        if not self.is_available:
            # Warn once: this is a configuration state, not a per-document fault.
            if not self._unavailable_warned:
                self._unavailable_warned = True
                logger.warning(
                    "No embedding provider available (provider=%r). Install "
                    "sentence-transformers or set OPENAI_API_KEY; vector "
                    "indexing is skipped. This message is logged once.",
                    self.provider,
                )
            return []

        logger.info(
            "Generating %d dense embeddings via %s (model=%s, dims=%d)...",
            len(chunks), self.provider, self.model, self.dimensions,
        )
        try:
            if self.provider == "sentence-transformers":
                vectors = await asyncio.to_thread(self._encode_local_sync, chunks)
            else:
                vectors = await self._encode_openai(chunks)
        except Exception as exc:
            logger.error("Failed to generate dense vectors: %s", exc)
            return []

        return self._validate(vectors, len(chunks))

    def _validate(self, vectors: List[List[float]], expected: int) -> List[List[float]]:
        """Confirms count and dimensionality before vectors reach a database."""
        if len(vectors) != expected:
            logger.error(
                "Embedding count mismatch: got %d, expected %d", len(vectors), expected
            )
            return []
        bad = next((i for i, v in enumerate(vectors) if len(v) != self.dimensions), None)
        if bad is not None:
            logger.error(
                "Embedding %d has %d dimensions, expected %d. Align "
                "EMBEDDING_DIMENSIONS with your Qdrant collection.",
                bad, len(vectors[bad]), self.dimensions,
            )
            return []
        logger.info("Generated %d dense embeddings.", len(vectors))
        return vectors

    # --- provider backends --------------------------------------------------
    def _load_local_model(self) -> Any:
        """Imports sentence-transformers and loads the model (blocking)."""
        from sentence_transformers import SentenceTransformer  # deferred: heavy import

        logger.info("Loading local embedding model '%s'...", self.model)
        return SentenceTransformer(self.model)

    async def _get_local_model(self) -> Any:
        """Loads the local model once, guarding against concurrent loads."""
        if self._local_model is None:
            async with self._local_lock:
                if self._local_model is None:
                    self._local_model = await asyncio.to_thread(self._load_local_model)
        return self._local_model

    def _encode_local_sync(self, chunks: List[str]) -> List[List[float]]:
        """Blocking sentence-transformers encode, run off the event loop."""
        model = self._local_model
        if model is None:
            raise RuntimeError("Local embedding model is not loaded")
        encoded = model.encode(
            chunks,
            convert_to_numpy=True,
            show_progress_bar=False,
            normalize_embeddings=False,
        )
        return [[float(value) for value in vector] for vector in encoded]

    def _get_openai_client(self) -> Any:
        """Builds (once) the AsyncOpenAI client or returns the injected one."""
        if self._openai_client is None:
            if self.embedding_client is not None:
                self._openai_client = self.embedding_client
            else:
                from openai import AsyncOpenAI  # deferred: optional dependency

                self._openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        return self._openai_client

    async def _encode_openai(self, chunks: List[str]) -> List[List[float]]:
        """Calls the OpenAI embeddings endpoint in batches."""
        client = self._get_openai_client()
        # `dimensions` is only honoured by the text-embedding-3 family.
        supports_dimensions = str(self.model).startswith("text-embedding-3")

        vectors: List[List[float]] = []
        for start in range(0, len(chunks), self.batch_size):
            batch = chunks[start : start + self.batch_size]
            kwargs: Dict[str, Any] = {"model": self.model, "input": batch}
            if supports_dimensions:
                kwargs["dimensions"] = self.dimensions
            response = await client.embeddings.create(**kwargs)
            vectors.extend([list(map(float, item.embedding)) for item in response.data])
        return vectors

    # --- sparse vectors -----------------------------------------------------
    def generate_sparse_tokens(self, text: str) -> Dict[str, float]:
        """Generates BM25-like sparse weight values for keyword lookup search systems."""
        words = (text or "").lower().split()
        total_words = len(words)
        if total_words == 0:
            return {}

        freqs = {}
        for word in words:
            freqs[word] = freqs.get(word, 0) + 1

        # Return term frequencies normalized
        return {word: float(count / total_words) for word, count in freqs.items()}
