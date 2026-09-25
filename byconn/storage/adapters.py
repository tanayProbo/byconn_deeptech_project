"""Asynchronous database adapters for the BYCONN-X storage layer.

Replaces the previous logging-only stubs with real, connection-backed adapters:

* :class:`PostgresAdapter`  - asyncpg pool, auto-creates its own schema.
* :class:`QdrantAdapter`    - AsyncQdrantClient, ``byconn_chunks`` collection.
* :class:`Neo4jAdapter`     - AsyncGraphDatabase, parameterized Cypher MERGEs.

Every connection parameter is read from the environment and falls back to a
standard local-development default, so the adapters can be constructed with no
arguments at all. Connection establishment is lazy and guarded by a lock, so
concurrent tasks share a single pool/driver/client instead of racing to build
duplicate ones.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

import asyncpg
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

try:  # python-dotenv is declared in requirements.txt; never fail if absent.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - convenience only
    pass

logger = logging.getLogger("byconnx.storage.adapters")

# --- Defaults -----------------------------------------------------------------
DEFAULT_PG_DSN = "postgresql://postgres:postgres@localhost:5432/byconnx"
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "password"

# Matches the embedder's dense output size, which drives the Qdrant vector width.
DEFAULT_VECTOR_SIZE = 384
DEFAULT_COLLECTION = "byconn_chunks"

# Namespaces keep derived point IDs deterministic, so re-upserting the same
# chunk overwrites its previous vector instead of duplicating it.
_POINT_ID_NAMESPACE = uuid.UUID("6f2a1f1e-9a1c-5f4b-9d3e-2b7c8a0d4e11")


def _env_str(key: str, default: str) -> str:
    """Reads a string env var, treating blank values as unset."""
    value = os.getenv(key)
    return value if value else default


def _env_int(key: str, default: int) -> int:
    """Reads an int env var, falling back on absence or malformed input."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using default %s", key, raw, default)
        return default


def _env_float(key: str, default: float) -> float:
    """Reads a float env var, falling back on absence or malformed input."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using default %s", key, raw, default)
        return default


def _env_opt(key: str) -> Optional[str]:
    """Reads an optional secret, returning None when unset or blank."""
    value = os.getenv(key)
    return value if value else None


def _env_bool(key: str, default: bool = False) -> bool:
    """Reads a boolean env var from the usual truthy spellings."""
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _postgres_dsn() -> str:
    """Resolves a DSN, preferring DATABASE_URL over discrete PG* variables."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    return (
        "postgresql://{user}:{password}@{host}:{port}/{database}".format(
            user=_env_str("POSTGRES_USER", "postgres"),
            password=_env_str("POSTGRES_PASSWORD", "postgres"),
            host=_env_str("POSTGRES_HOST", "localhost"),
            port=_env_str("POSTGRES_PORT", "5432"),
            database=_env_str("POSTGRES_DB", "byconnx"),
        )
    )


def _qdrant_url() -> str:
    """Resolves the Qdrant endpoint, preferring QDRANT_URL over host/port."""
    url = os.getenv("QDRANT_URL")
    if url:
        return url
    scheme = "https" if _env_bool("QDRANT_HTTPS", False) else "http"
    return "{}://{}:{}".format(
        scheme,
        _env_str("QDRANT_HOST", "localhost"),
        _env_str("QDRANT_PORT", "6333"),
    )


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _safe_identifier(raw: str, kind: str) -> str:
    """Validates a Cypher label or relationship type before interpolation.

    Cypher does not allow bound parameters for identifiers, so labels and
    relationship types must be inlined into the query string. Everything
    user-supplied is therefore run through this allowlist before it reaches
    the server; all *values* are still passed as query parameters.
    """
    if not isinstance(raw, str):
        raise ValueError(f"Cypher {kind} must be a string, got {type(raw).__name__}")
    candidate = raw.strip().replace(" ", "_").upper() if kind == "relationship type" else raw.strip()
    if not _IDENTIFIER_RE.match(candidate):
        raise ValueError(f"Invalid Cypher {kind}: {raw!r}")
    return candidate


class BaseAdapter:
    """Shared lazy-connection plumbing for the concrete storage adapters.

    Subclasses implement ``_connect`` and ``_close``. Callers may either
    ``await adapter.connect()`` up front or let the first operation connect on
    demand; both paths are serialized by ``_lock`` so only one connection is
    ever built. Adapters are also usable as async context managers.
    """

    def __init__(self, retry_backoff: float = 5.0) -> None:
        self._connected = False
        self._lock = asyncio.Lock()
        # After a failed connect, skip straight to the error for this long so a
        # health probe against a down backend returns immediately instead of
        # waiting out the full connect timeout on every request.
        self.retry_backoff = retry_backoff
        self._last_failure_at = 0.0

    @property
    def is_connected(self) -> bool:
        """Reports whether the underlying client is currently established."""
        return self._connected

    async def connect(self) -> "BaseAdapter":
        """Establishes the connection if it is not already open."""
        if self._connected:
            return self
        async with self._lock:
            if self._connected:
                return self
            if (
                self._last_failure_at
                and (time.monotonic() - self._last_failure_at) < self.retry_backoff
            ):
                raise ConnectionError(
                    f"{type(self).__name__}: not retried within "
                    f"{self.retry_backoff}s of the last failure"
                )
            try:
                await self._connect()
            except Exception:
                self._last_failure_at = time.monotonic()
                raise
            self._last_failure_at = 0.0
            self._connected = True
        return self

    async def _ensure(self) -> "BaseAdapter":
        """Connects on demand; the entry point every public method calls."""
        if not self._connected:
            await self.connect()
        return self

    async def ping(self) -> bool:
        """Verifies the backend is reachable. Returns False instead of raising."""
        await self._ensure()
        try:
            return await self._ping()
        except Exception as exc:
            logger.warning("%s ping failed: %s", type(self).__name__, exc)
            return False

    async def close(self) -> None:
        """Tears down the connection. Safe to call when never connected."""
        if not self._connected:
            return
        async with self._lock:
            if self._connected:
                try:
                    await self._close()
                finally:
                    self._connected = False

    async def __aenter__(self) -> "BaseAdapter":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --- subclass hooks -----------------------------------------------------
    async def _connect(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _ping(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


# --- Schema DDL ---------------------------------------------------------------

# Applied on every connect(); both statements are idempotent.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crawled_pages (
    id            BIGSERIAL PRIMARY KEY,
    url           TEXT        NOT NULL,
    url_hash      TEXT        NOT NULL UNIQUE,
    job_id        TEXT,
    title         TEXT,
    markdown      TEXT,
    chunk_count   INTEGER     NOT NULL DEFAULT 0,
    status_code   INTEGER,
    depth         INTEGER     NOT NULL DEFAULT 0,
    content_hash  TEXT,
    crawled_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crawled_pages_url_hash ON crawled_pages (url_hash);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_job_id    ON crawled_pages (job_id);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_crawled_at ON crawled_pages (crawled_at DESC);

CREATE TABLE IF NOT EXISTS extracted_entities (
    id            BIGSERIAL PRIMARY KEY,
    page_id       BIGINT      REFERENCES crawled_pages (id) ON DELETE CASCADE,
    name          TEXT        NOT NULL,
    entity_type   TEXT        NOT NULL DEFAULT 'ENTITY',
    source_url    TEXT,
    properties    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_extracted_entities_page_id     ON extracted_entities (page_id);
CREATE INDEX IF NOT EXISTS idx_extracted_entities_name       ON extracted_entities (name);
CREATE INDEX IF NOT EXISTS idx_extracted_entities_entity_type ON extracted_entities (entity_type);

-- Availability probes for catalogued public APIs (replaces the ClickHouse sink).
CREATE TABLE IF NOT EXISTS api_health_checks (
    id            BIGSERIAL PRIMARY KEY,
    api_id        TEXT        NOT NULL,
    api_name      TEXT,
    url           TEXT        NOT NULL,
    status_code   INTEGER     NOT NULL DEFAULT 0,
    latency_ms    INTEGER     NOT NULL DEFAULT 0,
    is_up         BOOLEAN     NOT NULL DEFAULT FALSE,
    error_message TEXT,
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_health_checks_api_id  ON api_health_checks (api_id);
CREATE INDEX IF NOT EXISTS idx_api_health_checks_time    ON api_health_checks (checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_health_checks_up      ON api_health_checks (is_up);

-- REST endpoints sniffed from live browser traffic by ProxySniffer.
CREATE TABLE IF NOT EXISTS discovered_apis (
    id              BIGSERIAL PRIMARY KEY,
    method          TEXT        NOT NULL,
    url             TEXT        NOT NULL,
    host            TEXT,
    path            TEXT,
    content_type    TEXT,
    sample_request  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    sample_response JSONB       NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count      INTEGER     NOT NULL DEFAULT 1,
    UNIQUE (method, url)
);

CREATE INDEX IF NOT EXISTS idx_discovered_apis_host ON discovered_apis (host);
CREATE INDEX IF NOT EXISTS idx_discovered_apis_path ON discovered_apis (path);
"""


def _as_jsonb(value: Any) -> Any:
    """Coerces a sniffed payload into something the jsonb codec accepts.

    Payloads arrive as text, dicts, lists or None. The registered jsonb encoder
    is ``json.dumps``, so anything that is not already a JSON-compatible
    container is wrapped or stringified rather than crashing the insert.
    """
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (str, int, float, bool)):
        return {"raw": value}
    return {"raw": str(value)}


def url_fingerprint(url: str) -> str:
    """Hashes a URL for use as a stable dedup key in PostgreSQL."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


class PostgresAdapter(BaseAdapter):
    """Async PostgreSQL adapter for crawled pages and extracted entities.

    Builds a lazily created :mod:`asyncpg` pool from ``DATABASE_URL`` (falling
    back to ``POSTGRES_HOST``/``PORT``/``USER``/``PASSWORD``/``DB``), and runs
    the ``crawled_pages`` / ``extracted_entities`` DDL on first connect. A
    ``jsonb`` codec is registered on every pooled connection so JSON columns
    round-trip as native ``dict`` objects rather than strings.
    """

    def __init__(
        self,
        dsn: Optional[str] = None,
        min_size: int = 1,
        max_size: int = 10,
        command_timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self.dsn = dsn or _postgres_dsn()
        self.min_size = max(1, min_size or _env_int("POSTGRES_POOL_MIN", 1))
        self.max_size = max(self.min_size, max_size or _env_int("POSTGRES_POOL_MAX", 10))
        self.command_timeout = command_timeout or _env_float("POSTGRES_COMMAND_TIMEOUT", 30.0)
        self._pool: Optional[asyncpg.Pool] = None

    # --- lifecycle ----------------------------------------------------------
    async def _connect(self) -> None:
        logger.info("Connecting to PostgreSQL at %s", _redact(self.dsn))
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
            command_timeout=self.command_timeout,
            init=self._init_connection,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info("PostgreSQL pool ready and schema ensured.")

    async def _close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        logger.info("PostgreSQL pool closed.")

    async def _ping(self) -> bool:
        async with self._require_pool().acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        """Maps jsonb to/from dicts on each new pooled connection."""
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresAdapter pool is not initialized; call connect() first.")
        return self._pool

    # --- writes -------------------------------------------------------------
    async def upsert_crawled_page(
        self,
        url: str,
        markdown: str = "",
        title: str = "",
        job_id: str = "",
        chunk_count: int = 0,
        status_code: int = 200,
        depth: int = 0,
        content_hash: str = "",
    ) -> int:
        """Inserts or updates a crawled page keyed on its URL hash.

        Returns the ``crawled_pages.id`` so callers can attach extracted
        entities to the same row.
        """
        await self._ensure()
        fingerprint = url_fingerprint(url)
        digest = content_hash or hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        row = await self._require_pool().fetchrow(
            """
            INSERT INTO crawled_pages
                (url, url_hash, job_id, title, markdown, chunk_count,
                 status_code, depth, content_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (url_hash) DO UPDATE SET
                job_id       = EXCLUDED.job_id,
                title        = EXCLUDED.title,
                markdown     = EXCLUDED.markdown,
                chunk_count  = EXCLUDED.chunk_count,
                status_code  = EXCLUDED.status_code,
                depth        = EXCLUDED.depth,
                content_hash = EXCLUDED.content_hash,
                crawled_at   = NOW()
            RETURNING id
            """,
            url,
            fingerprint,
            job_id or None,
            title or None,
            markdown,
            chunk_count,
            status_code,
            depth,
            digest,
        )
        logger.info("Upserted crawled page %s (id=%s)", url, row["id"])
        return row["id"]

    async def insert_entities(
        self,
        page_id: Optional[int],
        entities: Sequence[Dict[str, Any]],
        source_url: str = "",
    ) -> int:
        """Bulk-inserts extracted entities, optionally linked to a page row.

        Each entity accepts ``name``, ``entity_type`` and an optional
        ``properties`` mapping. Entities with no name are skipped. Returns the
        number of rows written.
        """
        await self._ensure()
        rows: List[tuple] = []
        for entity in entities or []:
            if not isinstance(entity, dict):
                logger.warning("Skipping non-dict entity payload: %r", type(entity).__name__)
                continue
            name = (entity.get("name") or "").strip()
            if not name:
                continue
            rows.append(
                (
                    page_id,
                    name,
                    (entity.get("entity_type") or entity.get("type") or "ENTITY").strip().upper(),
                    entity.get("source_url") or source_url or None,
                    entity.get("properties") or entity.get("metadata") or {},
                )
            )
        if not rows:
            return 0
        await self._require_pool().executemany(
            """
            INSERT INTO extracted_entities
                (page_id, name, entity_type, source_url, properties)
            VALUES ($1, $2, $3, $4, $5)
            """,
            rows,
        )
        logger.info("Inserted %d entities for page_id=%s", len(rows), page_id)
        return len(rows)

    async def record_api_health(
        self,
        api_id: str,
        url: str,
        status_code: int = 0,
        latency_ms: int = 0,
        is_up: bool = False,
        error_message: str = "",
        api_name: str = "",
    ) -> None:
        """Appends one availability probe result for a catalogued API.

        Accepts the report shape produced by
        :meth:`~byconn.free_api_integration.health_monitor.APIHealthMonitor.check_api`.
        """
        await self._ensure()
        await self._require_pool().execute(
            """
            INSERT INTO api_health_checks
                (api_id, api_name, url, status_code, latency_ms, is_up, error_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            api_id,
            api_name or None,
            url,
            int(status_code or 0),
            int(latency_ms or 0),
            bool(is_up),
            error_message or None,
        )
        logger.info("Recorded api health %s -> %s (%sms)", api_id, status_code, latency_ms)

    async def latest_api_health(self, api_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """Returns recent health probes, newest first, optionally per API."""
        await self._ensure()
        rows = await self._require_pool().fetch(
            """
            SELECT api_id, api_name, url, status_code, latency_ms, is_up,
                   error_message, checked_at
            FROM api_health_checks
            WHERE ($1::TEXT IS NULL OR api_id = $1)
            ORDER BY checked_at DESC
            LIMIT $2
            """,
            api_id or None,
            limit,
        )
        return [dict(row) for row in rows]

    async def register_discovered_api(self, endpoint: Dict[str, Any]) -> None:
        """Upserts one sniffed endpoint, incrementing ``seen_count``.

        Accepts the dict produced by
        :meth:`~byconn.api_intelligence.proxy_sniffer.DiscoveredEndpoint.to_dict`.
        """
        await self._ensure()
        await self._require_pool().execute(
            """
            INSERT INTO discovered_apis
                (method, url, host, path, content_type, sample_request, sample_response)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (method, url) DO UPDATE SET
                content_type    = EXCLUDED.content_type,
                sample_request  = EXCLUDED.sample_request,
                sample_response = EXCLUDED.sample_response,
                last_seen_at    = NOW(),
                seen_count      = discovered_apis.seen_count + 1
            """,
            (endpoint.get("method") or "GET").upper(),
            endpoint.get("url") or "",
            endpoint.get("host") or None,
            endpoint.get("path") or None,
            endpoint.get("content_type") or None,
            _as_jsonb(endpoint.get("sample_request")),
            _as_jsonb(endpoint.get("sample_response")),
        )

    async def register_discovered_apis(self, endpoints: Sequence[Dict[str, Any]]) -> int:
        """Bulk-upserts sniffed endpoints. Returns the number written."""
        written = 0
        for endpoint in endpoints or []:
            if not isinstance(endpoint, dict) or not endpoint.get("url"):
                continue
            try:
                await self.register_discovered_api(endpoint)
                written += 1
            except Exception as exc:
                logger.warning("Failed to record discovered API %s: %s", endpoint.get("url"), exc)
        return written

    async def list_discovered_apis(self, host: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        """Lists sniffed endpoints, most recently seen first."""
        await self._ensure()
        rows = await self._require_pool().fetch(
            """
            SELECT method, url, host, path, content_type, seen_count,
                   first_seen_at, last_seen_at
            FROM discovered_apis
            WHERE ($1::TEXT IS NULL OR host = $1)
            ORDER BY last_seen_at DESC
            LIMIT $2
            """,
            host or None,
            limit,
        )
        return [dict(row) for row in rows]

    # --- reads --------------------------------------------------------------
    async def get_crawled_page(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetches a single crawled page by URL, or None if unseen."""
        await self._ensure()
        row = await self._require_pool().fetchrow(
            "SELECT * FROM crawled_pages WHERE url_hash = $1",
            url_fingerprint(url),
        )
        return dict(row) if row else None

    async def list_crawled_pages(
        self,
        job_id: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Lists recently crawled pages, newest first, optionally per job."""
        await self._ensure()
        rows = await self._require_pool().fetch(
            """
            SELECT id, url, job_id, title, chunk_count, status_code, depth, crawled_at
            FROM crawled_pages
            WHERE ($1::TEXT IS NULL OR job_id = $1)
            ORDER BY crawled_at DESC
            LIMIT $2 OFFSET $3
            """,
            job_id or None,
            limit,
            offset,
        )
        return [dict(row) for row in rows]

    async def list_entities(self, page_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Lists extracted entities, optionally restricted to one page."""
        await self._ensure()
        rows = await self._require_pool().fetch(
            """
            SELECT id, page_id, name, entity_type, source_url, properties, created_at
            FROM extracted_entities
            WHERE ($1::BIGINT IS NULL OR page_id = $1)
            ORDER BY created_at DESC
            LIMIT $2
            """,
            page_id,
            limit,
        )
        return [dict(row) for row in rows]

    async def count_rows(self, table: str) -> int:
        """Returns the row count of a whitelisted table, for health checks."""
        allowed = {"crawled_pages", "extracted_entities", "api_health_checks", "discovered_apis"}
        if table not in allowed:
            raise ValueError(f"Unsupported table: {table!r}; expected one of {sorted(allowed)}")
        await self._ensure()
        return await self._require_pool().fetchval(f"SELECT COUNT(*) FROM {table}")


class QdrantAdapter(BaseAdapter):
    """Async Qdrant adapter storing document-chunk embeddings.

    Builds an :class:`AsyncQdrantClient` from ``QDRANT_URL`` (falling back to
    ``QDRANT_HOST``/``QDRANT_PORT``/``QDRANT_HTTPS``) and ensures a
    ``byconn_chunks`` collection exists with 384-dimension cosine vectors plus
    keyword payload indexes on ``url`` and ``job_id`` for filtered search.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        collection_name: str = DEFAULT_COLLECTION,
        vector_size: int = DEFAULT_VECTOR_SIZE,
        prefer_grpc: bool = False,
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self.url = url or _qdrant_url()
        self.api_key = api_key or _env_opt("QDRANT_API_KEY")
        self.collection_name = collection_name or _env_str("QDRANT_COLLECTION", DEFAULT_COLLECTION)
        self.vector_size = vector_size or _env_int("QDRANT_VECTOR_SIZE", DEFAULT_VECTOR_SIZE)
        self.prefer_grpc = prefer_grpc
        self.timeout = timeout
        self._client: Optional[AsyncQdrantClient] = None

    # --- lifecycle ----------------------------------------------------------
    async def _connect(self) -> None:
        logger.info("Connecting to Qdrant at %s", self.url)
        self._client = AsyncQdrantClient(
            url=self.url,
            api_key=self.api_key,
            prefer_grpc=self.prefer_grpc,
            timeout=self.timeout,
        )
        await self.ensure_collection()
        logger.info("Qdrant collection '%s' ready (%d dims, cosine).", self.collection_name, self.vector_size)

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        logger.info("Qdrant client closed.")

    async def _ping(self) -> bool:
        collections = await self._require_client().get_collections()
        return collections is not None

    def _require_client(self) -> AsyncQdrantClient:
        if self._client is None:
            raise RuntimeError("QdrantAdapter client is not initialized; call connect() first.")
        return self._client

    # --- collection management ---------------------------------------------
    async def ensure_collection(self, recreate: bool = False) -> str:
        """Creates the collection and its payload indexes if they are absent.

        Returns the collection name. Safe to call repeatedly.
        """
        await self._ensure()
        client = self._require_client()

        exists = await client.collection_exists(self.collection_name)
        if exists and recreate:
            await client.delete_collection(self.collection_name)
            exists = False
        if not exists:
            await client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.vector_size, distance=Distance.COSINE),
            )
            logger.info("Created Qdrant collection '%s'.", self.collection_name)

        for field in ("url", "job_id"):
            await client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )
        return self.collection_name

    # --- writes -------------------------------------------------------------
    @staticmethod
    def point_id(url: str, chunk_index: int) -> str:
        """Derives a stable UUID5 point ID from a URL and chunk ordinal.

        Deterministic IDs make re-indexing idempotent: the same chunk always
        lands on the same point and replaces its vector.
        """
        return str(uuid.uuid5(_POINT_ID_NAMESPACE, f"{url.strip()}#{int(chunk_index)}"))

    async def upsert_embeddings(
        self,
        url: str,
        chunks: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        job_id: str = "",
        payload_extra: Optional[Dict[str, Any]] = None,
        batch_size: int = 64,
        wait: bool = True,
    ) -> int:
        """Indexes chunk texts plus their dense vectors for similarity search.

        ``chunks`` and ``embeddings`` must be the same length. Each embedding is
        validated against the collection's vector size before any write, so a
        dimension mismatch fails fast instead of partially indexing a document.
        """
        await self._ensure()
        if len(chunks) != len(embeddings):
            raise ValueError(f"chunks/embeddings length mismatch: {len(chunks)} vs {len(embeddings)}")

        points: List[PointStruct] = []
        for index, (chunk, vector) in enumerate(zip(chunks, embeddings, strict=True)):
            if len(vector) != self.vector_size:
                raise ValueError(
                    f"Embedding {index} has {len(vector)} dimensions, "
                    f"expected {self.vector_size} for collection '{self.collection_name}'"
                )
            payload = {
                "url": url,
                "chunk_index": index,
                "chunk": chunk,
                "job_id": job_id,
            }
            if payload_extra:
                payload.update(payload_extra)
            points.append(
                PointStruct(id=self.point_id(url, index), vector=list(map(float, vector)), payload=payload)
            )

        if not points:
            return 0

        client = self._require_client()
        written = 0
        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            await client.upsert(collection_name=self.collection_name, points=batch, wait=wait)
            written += len(batch)
        logger.info("Upserted %d chunk vectors for %s", written, url)
        return written

    async def delete_page(self, url: str) -> None:
        """Removes every chunk vector belonging to one source URL."""
        await self._ensure()
        await self._require_client().delete(
            collection_name=self.collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="url", match=MatchValue(value=url))]
            ),
            wait=True,
        )
        logger.info("Deleted Qdrant chunks for %s", url)

    # --- reads --------------------------------------------------------------
    async def search(
        self,
        vector: Sequence[float],
        limit: int = 5,
        url_filter: str = "",
        job_id_filter: str = "",
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Runs a cosine nearest-neighbour search over the chunk collection.

        Uses ``query_points`` when the installed client supports it, falling
        back to the older ``search`` method for qdrant-client 1.8/1.9.
        """
        await self._ensure()
        if len(vector) != self.vector_size:
            raise ValueError(
                f"Query vector has {len(vector)} dimensions, expected {self.vector_size}"
            )

        conditions = []
        if url_filter:
            conditions.append(FieldCondition(key="url", match=MatchValue(value=url_filter)))
        if job_id_filter:
            conditions.append(FieldCondition(key="job_id", match=MatchValue(value=job_id_filter)))
        query_filter = Filter(must=conditions) if conditions else None

        client = self._require_client()
        if hasattr(client, "query_points"):
            response = await client.query_points(
                collection_name=self.collection_name,
                query=list(map(float, vector)),
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
                score_threshold=score_threshold,
            )
            hits = response.points
        else:  # qdrant-client < 1.10
            hits = await client.search(
                collection_name=self.collection_name,
                query_vector=list(map(float, vector)),
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
                score_threshold=score_threshold,
            )

        return [
            {"id": hit.id, "score": hit.score, "payload": hit.payload or {}}
            for hit in hits
        ]

    async def count_points(self, url_filter: str = "") -> int:
        """Counts indexed vectors, optionally scoped to one source URL."""
        await self._ensure()
        count_filter = None
        if url_filter:
            count_filter = Filter(must=[FieldCondition(key="url", match=MatchValue(value=url_filter))])
        result = await self._require_client().count(
            collection_name=self.collection_name,
            count_filter=count_filter,
            exact=True,
        )
        return result.count


class Neo4jAdapter(BaseAdapter):
    """Async Neo4j adapter for the extracted knowledge graph.

    Builds a driver from ``NEO4J_URI``/``NEO4J_USER``/``NEO4J_PASSWORD`` (and
    optional ``NEO4J_DATABASE``), and applies uniqueness constraints on first
    connect. Labels and relationship types are allowlisted before being
    inlined into Cypher; all node and edge *values* are bound as parameters so
    crawled content can never be interpreted as query syntax.
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        max_connection_pool_size: int = 50,
    ) -> None:
        super().__init__()
        self.uri = uri or _env_str("NEO4J_URI", DEFAULT_NEO4J_URI)
        self.user = user or _env_str("NEO4J_USER", DEFAULT_NEO4J_USER)
        self.password = password or _env_str("NEO4J_PASSWORD", DEFAULT_NEO4J_PASSWORD)
        self.database = database or _env_opt("NEO4J_DATABASE")
        self.max_connection_pool_size = max_connection_pool_size or _env_int("NEO4J_POOL_SIZE", 50)
        self._driver = None

    # --- lifecycle ----------------------------------------------------------
    async def _connect(self) -> None:
        logger.info("Connecting to Neo4j at %s", self.uri)
        self._driver = AsyncGraphDatabase.driver(
            self.uri,
            auth=(self.user, self.password),
            max_connection_pool_size=self.max_connection_pool_size,
        )
        await self._ensure_constraints()
        logger.info("Neo4j driver ready and constraints ensured.")

    async def _close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
        logger.info("Neo4j driver closed.")

    async def _ping(self) -> bool:
        async with self._require_driver().session(database=self.database) as session:
            result = await session.run("RETURN 1 AS ok")
            record = await result.single()
            return bool(record and record["ok"] == 1)

    def _require_driver(self):
        if self._driver is None:
            raise RuntimeError("Neo4jAdapter driver is not initialized; call connect() first.")
        return self._driver

    async def _ensure_constraints(self) -> None:
        """Creates the uniqueness constraints that make MERGE idempotent."""
        statements = [
            "CREATE CONSTRAINT constraint_page_url IF NOT EXISTS "
            "FOR (p:Page) REQUIRE p.url IS UNIQUE",
            "CREATE CONSTRAINT constraint_entity_key IF NOT EXISTS "
            "FOR (e:Entity) REQUIRE e.key IS UNIQUE",
        ]
        async with self._require_driver().session(database=self.database) as session:
            for statement in statements:
                try:
                    await session.run(statement)
                except Exception as exc:
                    # Older servers reject IF NOT EXISTS on constraints; the
                    # equivalent already-existing error is not fatal.
                    logger.warning("Constraint setup skipped (%s): %s", statement.split()[2], exc)

    # --- writes -------------------------------------------------------------
    async def upsert_page(
        self,
        url: str,
        title: str = "",
        job_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """MERGEs a :Page node for a crawled URL."""
        label = _safe_identifier("Page", "label")
        await self._ensure()
        async with self._require_driver().session(database=self.database) as session:
            await session.run(
                f"""
                MERGE (p:{label} {{url: $url}})
                ON CREATE SET p.created_at = timestamp()
                SET p.title = $title,
                    p.job_id = $job_id,
                    p.crawled_at = timestamp(),
                    p.metadata = $metadata
                """,
                url=url,
                title=title or None,
                job_id=job_id or None,
                metadata=metadata or {},
            )
        logger.info("MERGED Page node for %s", url)

    async def write_triple(
        self,
        subject: str,
        predicate: str,
        obj: str,
        subject_type: str = "Entity",
        object_type: str = "Entity",
        properties: Optional[Dict[str, Any]] = None,
    ) -> int:
        """MERGEs two nodes and the typed relationship between them.

        Returns 1 on success. Unlike :meth:`write_triples`, this validates its
        arguments eagerly and raises ``ValueError`` for a bad identifier, since
        a single explicit call is the caller's own responsibility.
        """
        return await self.write_triples(
            [
                {
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "subject_type": subject_type,
                    "object_type": object_type,
                }
            ],
            properties=properties,
            strict=True,
        )

    async def write_triples(
        self,
        triples: Sequence[Dict[str, Any]],
        properties: Optional[Dict[str, Any]] = None,
        strict: bool = False,
    ) -> int:
        """Writes many triples inside one managed transaction.

        Each entry accepts ``subject``, ``predicate``, ``object`` and optional
        ``subject_type`` / ``object_type``. Returns the number of triples
        written.

        Entries that are incomplete, or whose labels/relationship types fail
        the identifier allowlist, are logged and skipped so that a single bad
        extraction cannot discard an otherwise valid batch. Pass
        ``strict=True`` to raise on the first invalid entry instead.
        """
        await self._ensure()

        prepared = []
        for triple in triples or []:
            try:
                if not isinstance(triple, dict):
                    raise ValueError(f"expected a dict, got {type(triple).__name__}")
                subject = (triple.get("subject") or "").strip()
                predicate = (triple.get("predicate") or "").strip()
                obj = (triple.get("object") or "").strip()
                if not (subject and predicate and obj):
                    raise ValueError("subject, predicate and object are all required")
                prepared.append(
                    {
                        "subject": subject,
                        "predicate": _safe_identifier(predicate, "relationship type"),
                        "object": obj,
                        "subject_label": _safe_identifier(triple.get("subject_type") or "Entity", "label"),
                        "object_label": _safe_identifier(triple.get("object_type") or "Entity", "label"),
                    }
                )
            except ValueError as exc:
                if strict:
                    raise
                logger.warning("Skipping invalid triple (%s): %r", exc, triple)
        if not prepared:
            return 0

        props = properties or {}

        async def _txn(tx) -> None:
            for item in prepared:
                # Identifiers are allowlisted above; every value is a parameter.
                await tx.run(
                    f"""
                    MERGE (s:{item['subject_label']} {{key: $subject}})
                    ON CREATE SET s.name = $subject, s.created_at = timestamp()
                    MERGE (o:{item['object_label']} {{key: $object}})
                    ON CREATE SET o.name = $object, o.created_at = timestamp()
                    MERGE (s)-[r:{item['predicate']}]->(o)
                    SET r.updated_at = timestamp(),
                        r.properties = $properties
                    """,
                    subject=item["subject"],
                    object=item["object"],
                    properties=props,
                )

        async with self._require_driver().session(database=self.database) as session:
            await session.execute_write(_txn)
        logger.info("Wrote %d relationship triples to Neo4j.", len(prepared))
        return len(prepared)

    async def link_page_entities(
        self,
        url: str,
        entities: Sequence[Dict[str, Any]],
    ) -> int:
        """MERGEs entities and connects each to its source :Page node.

        Mirrors the graph view of a page that :class:`PostgresAdapter` stores
        relationally. Returns the number of entities linked.
        """
        await self._ensure()
        page_label = _safe_identifier("Page", "label")
        entity_label = _safe_identifier("Entity", "label")
        valid: List[Dict[str, Any]] = []
        for entity in entities or []:
            if not isinstance(entity, dict):
                continue
            name = (entity.get("name") or "").strip()
            if not name:
                continue
            valid.append(
                {
                    "name": name,
                    "type": (entity.get("entity_type") or entity.get("type") or "ENTITY").strip().upper(),
                }
            )
        if not valid:
            return 0

        async def _txn(tx) -> None:
            await tx.run(
                f"MERGE (p:{page_label} {{url: $url}}) ON CREATE SET p.created_at = timestamp()",
                url=url,
            )
            for item in valid:
                await tx.run(
                    f"""
                    MERGE (e:{entity_label} {{key: $name}})
                    ON CREATE SET e.name = $name, e.created_at = timestamp()
                    SET e.entity_type = $type
                    MERGE (p)-[r:MENTIONS]->(e)
                    SET r.updated_at = timestamp()
                    """,
                    name=item["name"],
                    type=item["type"],
                )

        async with self._require_driver().session(database=self.database) as session:
            await session.execute_write(_txn)
        logger.info("Linked %d entities to Page %s", len(valid), url)
        return len(valid)

    # --- reads --------------------------------------------------------------
    async def neighbors(self, entity_name: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Returns the one-hop neighbourhood of an entity as plain dicts."""
        label = _safe_identifier("Entity", "label")
        await self._ensure()
        async with self._require_driver().session(database=self.database) as session:
            result = await session.run(
                f"""
                MATCH (e:{label} {{key: $name}})-[r]-(n)
                RETURN n.key AS key, labels(n) AS labels, type(r) AS rel, properties(n) AS props
                LIMIT $limit
                """,
                name=entity_name,
                limit=limit,
            )
            records = await result.data()
        return [dict(record) for record in records]

    async def stats(self) -> Dict[str, int]:
        """Returns node and relationship counts, for health dashboards."""
        await self._ensure()
        async with self._require_driver().session(database=self.database) as session:
            result = await session.run(
                "MATCH (n) WITH count(n) AS nodes "
                "MATCH ()-[r]->() RETURN nodes, count(r) AS rels"
            )
            record = await result.single()
        if not record:
            return {"nodes": 0, "relationships": 0}
        return {"nodes": record["nodes"], "relationships": record["rels"]}


def _redact(dsn: str) -> str:
    """Masks the password in a PostgreSQL DSN before it reaches the logs."""
    return re.sub(r"://([^:]+):[^@]*@", r"://\1:***@", dsn)


__all__ = ["PostgresAdapter", "QdrantAdapter", "Neo4jAdapter", "BaseAdapter", "SCHEMA_SQL"]
