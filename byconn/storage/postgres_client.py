import logging
import uuid
from typing import Dict, Any, List, Optional

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

logger = logging.getLogger("byconnx.storage.postgres")

# DDL — run once on first connect
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crawler_configs (
    id          TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name        TEXT NOT NULL,
    start_urls  JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS crawl_jobs (
    id          TEXT PRIMARY KEY,
    config_id   TEXT REFERENCES crawler_configs(id),
    status      TEXT NOT NULL DEFAULT 'pending',
    pages_crawled INT DEFAULT 0,
    errors      INT DEFAULT 0,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS discovered_apis (
    id          SERIAL PRIMARY KEY,
    method      TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    host        TEXT,
    content_type TEXT,
    sample_request  TEXT,
    sample_response TEXT,
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (method, endpoint)
);
"""


class PostgresAdapter:
    """
    Manages transactional state (crawler configs, job tracking, discovered APIs) in PostgreSQL.
    """
    def __init__(self, connection_uri: str):
        self.connection_uri = connection_uri
        self.conn = None

    def connect(self):
        """Opens a psycopg2 connection and ensures schema exists."""
        if not PSYCOPG2_AVAILABLE:
            raise ImportError("psycopg2-binary not installed. Run: pip install psycopg2-binary")
        self.conn = psycopg2.connect(self.connection_uri)
        self.conn.autocommit = False
        with self.conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        self.conn.commit()
        logger.info("PostgreSQL connected and schema verified.")

    def _cursor(self):
        if self.conn is None or self.conn.closed:
            self.connect()
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def create_crawler_config(
        self,
        workspace_id: str,
        name: str,
        start_urls: List[str]
    ) -> str:
        """Saves a new crawler configuration and returns its generated ID."""
        config_id = str(uuid.uuid4())
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO crawler_configs (id, workspace_id, name, start_urls)
                VALUES (%s, %s, %s, %s)
                """,
                (config_id, workspace_id, name, psycopg2.extras.Json(start_urls))
            )
        self.conn.commit()
        logger.info(f"PostgreSQL: Created crawler config '{name}' → {config_id}")
        return config_id

    def create_job(self, config_id: str) -> str:
        """Creates a new crawl job record linked to a config."""
        job_id = str(uuid.uuid4())
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO crawl_jobs (id, config_id, status) VALUES (%s, %s, 'running')",
                (job_id, config_id)
            )
        self.conn.commit()
        logger.info(f"PostgreSQL: Created job {job_id}")
        return job_id

    def update_job_status(self, job_id: str, status: str, pages: int, errors: int):
        """Updates job status, page count, and error count."""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE crawl_jobs
                SET status=%s, pages_crawled=%s, errors=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (status, pages, errors, job_id)
            )
        self.conn.commit()
        logger.info(f"PostgreSQL: Job {job_id} → {status} (pages={pages}, errors={errors})")

    def register_discovered_api(self, api_metadata: Dict[str, Any]):
        """Upserts a newly discovered API endpoint."""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovered_apis
                    (method, endpoint, host, content_type, sample_request, sample_response)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (method, endpoint) DO UPDATE
                    SET sample_response = EXCLUDED.sample_response,
                        discovered_at = NOW()
                """,
                (
                    api_metadata.get("method", "GET"),
                    api_metadata.get("endpoint", ""),
                    api_metadata.get("host", ""),
                    api_metadata.get("content_type", ""),
                    api_metadata.get("sample_request"),
                    api_metadata.get("sample_response"),
                )
            )
        self.conn.commit()
        logger.info(f"PostgreSQL: Registered API {api_metadata.get('method')} {api_metadata.get('endpoint')}")

    def close(self):
        """Closes the database connection."""
        if self.conn and not self.conn.closed:
            self.conn.close()
            logger.info("PostgreSQL connection closed.")
