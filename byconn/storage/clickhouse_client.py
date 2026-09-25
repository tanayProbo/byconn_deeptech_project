import logging
import datetime
from typing import List, Dict, Any, Optional

try:
    import clickhouse_connect
    CLICKHOUSE_AVAILABLE = True
except ImportError:
    CLICKHOUSE_AVAILABLE = False

logger = logging.getLogger("byconnx.storage.clickhouse")

# DDL for the crawl_events table
_CREATE_CRAWL_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS crawl_events (
    event_time   DateTime DEFAULT now(),
    job_id       String,
    url          String,
    status_code  UInt16,
    response_time_ms UInt32,
    bytes_downloaded UInt64,
    proxy_used   String DEFAULT '',
    error_message String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY (job_id, event_time)
"""


class ClickHouseAdapter:
    """
    Manages telemetry writes and crawler metric aggregation queries in ClickHouse.
    Engineered for high-frequency logs and page stats analysis.
    """
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        database: str = "byconnx",
        username: str = "default",
        password: str = ""
    ):
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.client = None

    def connect(self):
        """Creates a clickhouse-connect HTTP client and ensures the schema exists."""
        if not CLICKHOUSE_AVAILABLE:
            raise ImportError("clickhouse-connect not installed. Run: pip install clickhouse-connect")
        self.client = clickhouse_connect.get_client(
            host=self.host,
            port=self.port,
            database=self.database,
            username=self.username,
            password=self.password
        )
        # Ensure table exists
        self.client.command(_CREATE_CRAWL_EVENTS_SQL)
        logger.info(f"Connected to ClickHouse '{self.database}' at {self.host}:{self.port}")

    def _get_client(self):
        if self.client is None:
            self.connect()
        return self.client

    def write_crawl_event(self, event: Dict[str, Any]):
        """Inserts a single crawl page event row into ClickHouse."""
        row = [[
            event.get("event_time", datetime.datetime.utcnow()),
            str(event.get("job_id", "")),
            str(event.get("url", "")),
            int(event.get("status_code", 0)),
            int(event.get("response_time_ms", 0)),
            int(event.get("bytes_downloaded", 0)),
            str(event.get("proxy_used", "")),
            str(event.get("error_message", ""))
        ]]
        self._get_client().insert(
            "crawl_events",
            row,
            column_names=[
                "event_time", "job_id", "url", "status_code",
                "response_time_ms", "bytes_downloaded", "proxy_used", "error_message"
            ]
        )
        logger.debug(f"ClickHouse: Inserted event for URL: {event.get('url')}")

    def batch_write_crawl_events(self, events: List[Dict[str, Any]]):
        """Bulk inserts multiple crawl events in one network call."""
        rows = [
            [
                e.get("event_time", datetime.datetime.utcnow()),
                str(e.get("job_id", "")),
                str(e.get("url", "")),
                int(e.get("status_code", 0)),
                int(e.get("response_time_ms", 0)),
                int(e.get("bytes_downloaded", 0)),
                str(e.get("proxy_used", "")),
                str(e.get("error_message", ""))
            ]
            for e in events
        ]
        self._get_client().insert(
            "crawl_events",
            rows,
            column_names=[
                "event_time", "job_id", "url", "status_code",
                "response_time_ms", "bytes_downloaded", "proxy_used", "error_message"
            ]
        )
        logger.info(f"ClickHouse: Batch-inserted {len(rows)} crawl events.")

    def query_crawl_rates(self, job_id: str) -> List[Dict[str, Any]]:
        """Returns per-minute throughput and average latency for a given job."""
        sql = f"""
        SELECT
            toStartOfMinute(event_time) AS minute,
            count()                     AS count,
            avg(response_time_ms)       AS avg_latency
        FROM crawl_events
        WHERE job_id = '{job_id}'
        GROUP BY minute
        ORDER BY minute ASC
        """
        result = self._get_client().query(sql)
        return [
            {"minute": str(row[0]), "count": row[1], "avg_latency": round(row[2], 2)}
            for row in result.result_rows
        ]

    def close(self):
        """Closes the ClickHouse client."""
        if self.client:
            self.client.close()
            logger.info("ClickHouse connection closed.")
