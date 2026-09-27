"""BYCONN-X FastAPI service.

Exposes the data acquisition pipeline over HTTP:

* ``POST /api/v1/crawl``      - queue a crawl; returns a job id immediately.
* ``GET  /api/v1/crawl/{id}`` - poll job status and counters.
* ``GET  /api/v1/health``     - per-dependency status (503 if any is down).
* ``GET  /dashboard/``        - the static operator console, when present.

Each accepted crawl runs as a background task that walks the site with the
Playwright engine in :mod:`byconn.core`, cleans every page with
:meth:`~byconn.pipeline.cleaning.DataCleaner.clean_html`, stores the raw page in
PostgreSQL, indexes chunk embeddings in Qdrant, extracts structured knowledge
with :class:`~byconn.pipeline.llm_extractor.LLMExtractor`, and writes the
resulting relations into Neo4j.
"""

import asyncio
import csv
import io
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from dotenv import load_dotenv

# Environment must be loaded before any adapter reads its configuration.
load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Response, status  # noqa: E402
from fastapi.responses import RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, field_validator  # noqa: E402

from byconn.api_intelligence.proxy_sniffer import ProxySniffer  # noqa: E402
from byconn.api_intelligence.schema_generator import SchemaGenerator  # noqa: E402
from byconn.core.browser_pool import BrowserPool  # noqa: E402
from byconn.core.crawler import BaseCrawler  # noqa: E402
from byconn.core.link_discovery import (  # noqa: E402
    extract_links,
    is_allowed,
    load_robots,
)
from byconn.core.request_queue import CrawlRequest, RequestQueue  # noqa: E402
from byconn.core.session_pool import SessionPool  # noqa: E402
from byconn.pipeline.cleaning import DataCleaner  # noqa: E402
from byconn.pipeline.embedder import DEFAULT_CHUNK_WORDS, DocumentEmbedder  # noqa: E402
from byconn.pipeline import structured  # noqa: E402
from byconn.pipeline.llm_extractor import LLMExtractor  # noqa: E402
from byconn.storage.adapters import (  # noqa: E402
    Neo4jAdapter,
    PostgresAdapter,
    QdrantAdapter,
)
from byconn.visual_agent.agent_loop import VisualBrowserAgent  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("byconnx.server")

VERSION = "0.1.0"
DASHBOARD_DIR = Path(__file__).resolve().parent / "byconn" / "dashboard"
STARTED_AT = time.time()

# --- tunables (all overridable via environment) -------------------------------
def _env_str(key: str, default: str) -> str:
    """Reads a string env var, treating blank values as unset."""
    raw = os.getenv(key)
    return raw if raw else default


def _env_int(key: str, default: int) -> int:
    """Reads an int env var, falling back on absence or malformed input."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using %s", key, raw, default)
        return default


def _env_float(key: str, default: float) -> float:
    """Reads a float env var, falling back on absence or malformed input."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using %s", key, raw, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Reads a boolean env var from the usual truthy spellings."""
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


MAX_CRAWL_CONCURRENCY = max(1, _env_int("CRAWL_CONCURRENCY", 2))
CRAWL_PAGE_CONCURRENCY = max(1, _env_int("CRAWL_PAGE_CONCURRENCY", 3))
CRAWL_MAX_PAGES = max(1, _env_int("CRAWL_MAX_PAGES", 50))
CRAWL_HEADLESS = _env_bool("CRAWL_HEADLESS", True)
CRAWL_MAX_MEMORY_PERCENT = _env_float("CRAWL_MAX_MEMORY_PERCENT", 90.0)
# Words per chunk; see DEFAULT_CHUNK_WORDS for why it must stay under ~190.
CHUNK_SIZE = max(20, _env_int("CHUNK_SIZE", DEFAULT_CHUNK_WORDS))

MAX_JOBS_RETAINED = max(1, _env_int("MAX_JOBS_RETAINED", 200))
STARTUP_CONNECT_TIMEOUT = max(0.5, _env_float("STARTUP_CONNECT_TIMEOUT", 10.0))
PIPELINE_STEP_TIMEOUT = max(1.0, _env_float("PIPELINE_STEP_TIMEOUT", 30.0))
# Health must answer quickly, so probes get a much shorter budget than
# startup; adapters also back off after a failure.
HEALTH_PROBE_TIMEOUT = max(0.5, _env_float("HEALTH_PROBE_TIMEOUT", 2.0))
# Upper bound on processing one page: two concurrent phases of pipeline steps
# (the Neo4j phase runs three in sequence), plus slack for embedding.
PAGE_HANDLER_TIMEOUT = max(
    5.0, _env_float("PAGE_HANDLER_TIMEOUT", PIPELINE_STEP_TIMEOUT * 5 + 30)
)
# Structured extraction may run several windows plus a retry per window.
STRUCTURED_STEP_TIMEOUT = PIPELINE_STEP_TIMEOUT * 2
# Idle SSE streams send a comment this often so proxies keep them open.
SSE_HEARTBEAT_SECONDS = max(0.05, _env_float("SSE_HEARTBEAT_SECONDS", 15.0))
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
# Largest knowledge graph returned to the dashboard.
GRAPH_NODE_CAP = max(10, _env_int("GRAPH_NODE_CAP", 300))
# Spreadsheet apps execute cells starting with these (CSV injection).
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
DEDUPLICATE_PAGES = _env_bool("CRAWL_DEDUPLICATE", True)
API_INTELLIGENCE = _env_bool("API_INTELLIGENCE", True)


# =============================================================================
# Schemas
# =============================================================================
class CrawlBody(BaseModel):
    """Request payload for POST /api/v1/crawl."""

    url: str = Field(..., description="Absolute http(s) URL to start crawling from.")
    max_depth: int = Field(
        default=1,
        ge=0,
        le=5,
        description="Link-following depth. 0 crawls only the seed URL.",
    )
    prompt: str = Field(
        default="",
        max_length=2000,
        description="What to extract, in plain words, e.g. 'every pricing plan'.",
    )
    # Named json_schema because `schema` shadows a BaseModel attribute; clients
    # still send it as "schema".
    json_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="schema",
        description="Optional JSON Schema (type: object) the extracted data must match.",
    )

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        """Rejects anything that is not an absolute http(s) URL."""
        candidate = (value or "").strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("url must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("url must include a host, e.g. https://example.com")
        return candidate

    @field_validator("json_schema")
    @classmethod
    def _validate_schema(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Rejects schemas that are invalid, not for an object, or too large."""
        return None if value is None else structured.validate_schema(value)


class CrawlAccepted(BaseModel):
    """Response returned when a crawl is queued."""

    job_id: str
    status: str
    url: str
    max_depth: int
    pages_queued: int
    prompt: str = ""
    has_schema: bool = False


class ActionBody(BaseModel):
    """Request payload for POST /api/v1/act."""

    url: str = Field(..., description="Absolute http(s) URL to open.")
    task: str = Field(..., min_length=1, description="What the agent should accomplish.")
    max_steps: int = Field(
        default=10, ge=1, le=50, description="Maximum planner iterations."
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        """Rejects anything that is not an absolute http(s) URL."""
        candidate = (value or "").strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("url must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("url must include a host, e.g. https://example.com")
        return candidate


class ActionAccepted(BaseModel):
    """Response returned when an agent task is queued."""

    job_id: str
    status: str
    url: str
    task: str


class ComponentStatus(BaseModel):
    """Health of a single dependency."""

    name: str
    status: str
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    """Aggregate service health."""

    status: str
    version: str
    uptime_seconds: float
    llm: ComponentStatus
    embeddings: ComponentStatus
    components: List[ComponentStatus]
    active_jobs: int


# =============================================================================
# Job tracking
# =============================================================================
class EventHub:
    """Fans a job's progress events out to live subscribers (SSE clients).

    Publishing never blocks the pipeline: a subscriber that falls
    :data:`EVENT_QUEUE_SIZE` events behind loses events, and its stream then
    relies on the terminal-status check to finish.
    """

    EVENT_QUEUE_SIZE = 1000

    def __init__(self) -> None:
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.EVENT_QUEUE_SIZE)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: Any) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                logger.warning("Dropping %s event for a slow subscriber", event)


@dataclass
class CrawlJob:
    """In-memory record of one background crawl."""

    job_id: str
    url: str
    max_depth: int
    status: str = "queued"
    pages_crawled: int = 0
    pages_saved: int = 0
    duplicates_skipped: int = 0
    chunks_indexed: int = 0
    entities_extracted: int = 0
    relations_written: int = 0
    endpoints_discovered: int = 0
    errors: List[str] = field(default_factory=list)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    prompt: str = ""
    schema: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    # Per-page LLM results, in completion order (bounded by CRAWL_MAX_PAGES).
    pages: List[Dict[str, Any]] = field(default_factory=list)
    # Structured data merged across pages, with citations.
    structured: Optional[Dict[str, Any]] = None
    events: EventHub = field(default_factory=EventHub, repr=False)
    errors_published: int = 0
    kind: str = "crawl"

    @property
    def wants_structured(self) -> bool:
        """Whether the job asked for schema-driven extraction."""
        return bool(self.prompt or self.schema)

    def to_dict(self) -> Dict[str, Any]:
        """Returns a JSON-serialisable view of the job."""
        verified, unverified = structured.count_fields(self.structured)
        return {
            "kind": self.kind,
            "created_at": self.created_at,
            "job_id": self.job_id,
            "url": self.url,
            "max_depth": self.max_depth,
            "prompt": self.prompt,
            "has_schema": self.schema is not None,
            "fields_verified": verified,
            "fields_unverified": unverified,
            "status": self.status,
            "pages_crawled": self.pages_crawled,
            "pages_saved": self.pages_saved,
            "duplicates_skipped": self.duplicates_skipped,
            "chunks_indexed": self.chunks_indexed,
            "entities_extracted": self.entities_extracted,
            "relations_written": self.relations_written,
            "endpoints_discovered": self.endpoints_discovered,
            "errors": self.errors[:20],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": (
                round(self.finished_at - self.started_at, 2)
                if self.started_at and self.finished_at
                else None
            ),
        }


# =============================================================================
# Crawl helpers
# =============================================================================
async def guarded_step(
    job: CrawlJob, url: str, label: str, awaitable: Any, timeout: Optional[float] = None,
) -> Any:
    """Runs one downstream pipeline step under a timeout.

    Database clients retry internally, so without a bound a single dead
    dependency would stall a crawl job forever. Failures and timeouts are
    recorded on the job and swallowed: one bad step must not abort the crawl.
    """
    def _note(message: str) -> None:
        """Records a failure on the job when one was supplied."""
        if job is not None:
            job.errors.append(message)

    limit = timeout or PIPELINE_STEP_TIMEOUT
    try:
        return await asyncio.wait_for(awaitable, timeout=limit)
    except asyncio.TimeoutError:
        _note(f"{url}: {label} timed out after {limit:.0f}s")
        logger.error("%s timed out after %.0fs for %s", label, limit, url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _note(f"{url}: {label} failed ({exc})")
        logger.exception("%s failed for %s", label, url)
    return None


async def process_page(
    job: CrawlJob,
    request: CrawlRequest,
    page: Any,
    queue: RequestQueue,
    host: str,
    robots: Optional[RobotFileParser],
) -> Optional[Dict[str, Any]]:
    """Handles one crawled page: clean, persist, extract, relate, enqueue.

    Returns the page's result entry (also appended to ``job.pages``), or None
    when the page was skipped.

    The raw HTML is what gets stored in PostgreSQL; the cleaned copy is what
    feeds entity extraction, chunking and link discovery. A failure in any
    downstream step is recorded against the job but never aborts the crawl.
    """
    url = request.url
    title = ""
    html = ""

    # Hard page cap. Enqueue-time checks alone are not enough: with N workers,
    # several links can already be queued when the cap is hit, so refuse to
    # process anything past the limit. Checked before pages_crawled increments
    # so the cap is a true upper bound on pages visited.
    if job.pages_crawled >= CRAWL_MAX_PAGES:
        logger.info("Page cap %d reached; skipping %s", CRAWL_MAX_PAGES, url)
        return

    try:
        title = await page.title()
        html = await page.content()
    except Exception as exc:
        job.errors.append(f"{url}: failed to read page ({exc})")
        logger.exception("Failed to read page %s", url)
        return

    job.pages_crawled += 1

    markdown = ""
    if DEDUPLICATE_PAGES:
        # Cleaning plus near-duplicate suppression in one step: a page whose
        # content closely matches an earlier one is not stored, embedded or
        # sent to the LLM again.
        try:
            markdown = app.state.cleaner.html_to_unique_markdown(html)
        except Exception as exc:
            job.errors.append(f"{url}: cleaning failed ({exc})")
            logger.exception("cleaning failed for %s", url)
        if markdown is None:
            job.duplicates_skipped += 1
            logger.info("Skipping duplicate page %s", url)
            return
    else:
        try:
            # Required cleaning step: html_to_markdown strips scripts, styles
            # and layout boilerplate as part of its own pass.
            markdown = app.state.cleaner.html_to_markdown(html)
        except Exception as exc:
            job.errors.append(f"{url}: cleaning failed ({exc})")
            logger.exception("cleaning failed for %s", url)

    text_for_analysis = markdown or html

    # Chunking is cheap and synchronous; the page save needs the count.
    chunks: List[str] = []
    try:
        chunks = app.state.embedder.split_into_chunks(markdown or html)
    except Exception as exc:
        job.errors.append(f"{url}: chunking failed ({exc})")
        logger.exception("Failed to chunk %s", url)

    # The page save, vector indexing and LLM extraction are independent of
    # one another, so they run concurrently: a page costs the slowest of the
    # three (usually the LLM) rather than their sum. Every step still really
    # runs; each is timed out and recorded by guarded_step as before.
    async def save_page() -> Any:
        return await guarded_step(
            job, url, "postgres save",
            app.state.postgres.upsert_crawled_page(
                url=url,
                markdown=html,
                title=title,
                job_id=job.job_id,
                chunk_count=len(chunks),
                # The real response status; None (stored as NULL) if unknown.
                status_code=request.payload.get("status_code"),
                depth=request.depth,
            ),
        )

    async def index_chunks() -> Any:
        if not chunks:
            return None
        try:
            embeddings = await app.state.embedder.generate_dense_embeddings(chunks)
        except Exception as exc:
            job.errors.append(f"{url}: embedding failed ({exc})")
            logger.exception("Failed to embed %s", url)
            return None
        if not embeddings:
            return None
        return await guarded_step(
            job, url, "qdrant indexing",
            app.state.qdrant.upsert_embeddings(
                url=url,
                chunks=chunks,
                embeddings=embeddings,
                job_id=job.job_id,
                payload_extra={"title": title, "depth": request.depth},
            ),
        )

    async def extract() -> Any:
        if not app.state.extractor.is_available:
            return None
        return await guarded_step(
            job, url, "llm extraction",
            app.state.extractor.extract_knowledge(text_for_analysis),
        )

    async def extract_structured() -> Any:
        if not (job.wants_structured and app.state.extractor.is_available):
            return None
        return await guarded_step(
            job, url, "structured extraction",
            app.state.extractor.extract_structured(
                text_for_analysis, job.prompt, job.schema, url,
            ),
            timeout=STRUCTURED_STEP_TIMEOUT,
        )

    page_id, indexed, extracted, page_structured = await asyncio.gather(
        save_page(), index_chunks(), extract(), extract_structured(),
    )
    if page_id is not None:
        job.pages_saved += 1
    if indexed:
        job.chunks_indexed += indexed

    knowledge = extracted or app.state.extractor.empty_result()
    entities = knowledge.get("entities", [])
    triples = knowledge.get("triples", [])
    job.entities_extracted += len(entities)

    # --- store entities (PostgreSQL) and the graph (Neo4j) concurrently ---
    async def store_entities() -> None:
        if entities:
            await guarded_step(
                job, url, "entity insert",
                app.state.postgres.insert_entities(page_id, entities, source_url=url),
            )

    async def store_graph() -> None:
        if not triples:
            return
        await guarded_step(job, url, "neo4j page upsert",
                           app.state.neo4j.upsert_page(url, title=title, job_id=job.job_id))
        await guarded_step(job, url, "neo4j entity links",
                           app.state.neo4j.link_page_entities(url, entities))
        # Count only the triples Neo4j actually accepted. Counting the extracted
        # triples would report a healthy result while every write had failed.
        written = await guarded_step(
            job, url, "neo4j relations",
            app.state.neo4j.write_triples(
                triples,
                properties={"source_url": url, "job_id": job.job_id, "title": title},
            ),
        )
        job.relations_written += written or 0

    entry: Dict[str, Any] = {
        "url": url,
        "title": title,
        "status_code": request.payload.get("status_code"),
        "depth": request.depth,
        "summary": knowledge.get("summary", ""),
        "topics": knowledge.get("topics", []),
        "entities": entities,
        "triples": triples,
        "structured": page_structured,
        "model": getattr(app.state.extractor, "model", None),
    }
    job.pages.append(entry)
    if page_structured:
        if page_structured.get("windows_skipped"):
            job.errors.append(
                f"{url}: page too long; {page_structured['windows_skipped']} "
                "window(s) were not sent for structured extraction"
            )
        job.structured = structured.merge_structured([job.structured, page_structured])

    async def store_results() -> None:
        if knowledge.get("summary") or entities or page_structured:
            await guarded_step(
                job, url, "result insert",
                app.state.postgres.insert_extraction_result({**entry, "job_id": job.job_id}),
            )

    await asyncio.gather(store_entities(), store_graph(), store_results())

    # --- discover links for the next depth level --------------------------
    if request.depth < request.max_depth and job.pages_crawled < CRAWL_MAX_PAGES:
        for link in extract_links(html, url, host):
            if job.pages_crawled >= CRAWL_MAX_PAGES:
                logger.info("Page cap %d reached; not enqueueing %s", CRAWL_MAX_PAGES, link)
                break
            if not is_allowed(robots, link):
                logger.debug("robots.txt disallows %s", link)
                continue
            await queue.add(CrawlRequest(link, depth=request.depth + 1, max_depth=request.max_depth))
    elif request.depth >= request.max_depth:
        logger.info("Depth limit %d reached at %s", request.max_depth, url)
    return entry


async def publish_discovered_apis(job: CrawlJob, sniffer: ProxySniffer) -> None:
    """Persists endpoints sniffed during a crawl and generates an OpenAPI spec.

    Runs after the crawl finishes so the sniffer has seen every request the
    pages issued. Failures are recorded on the job but never fail the crawl.
    """
    generator = SchemaGenerator(
        service_title=f"Discovered API - {job.url}",
        version=VERSION,
    )
    # Page loads are captured alongside real API calls; an API registry and a
    # generated spec should describe only the latter.
    api_endpoints = [e for e in sniffer.endpoints if not generator.is_document(e)]
    job.endpoints_discovered = len(api_endpoints)
    if not api_endpoints:
        logger.info("Job %s: no API endpoints discovered.", job.job_id)
        return

    endpoints = [e.to_dict() for e in api_endpoints]

    try:
        written = await app.state.postgres.register_discovered_apis(endpoints)
        logger.info("Job %s: persisted %d discovered endpoints.", job.job_id, written)
    except Exception as exc:
        job.errors.append(f"discovered API persistence failed ({exc})")
        logger.exception("Failed to persist discovered APIs for %s", job.job_id)

    try:
        spec = generator.generate_openapi_spec(api_endpoints, skip_documents=False)
        target = Path(app.state.output_dir) / f"openapi_{job.job_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        logger.info("Job %s: wrote OpenAPI spec to %s (%d paths).",
                    job.job_id, target, len(spec.get("paths", {})))
    except Exception as exc:
        job.errors.append(f"openapi generation failed ({exc})")
        logger.exception("Failed to generate OpenAPI spec for %s", job.job_id)


async def run_crawl_pipeline(job: CrawlJob) -> None:
    """Drives one end-to-end crawl as a background task.

    Holds a global semaphore so only ``CRAWL_CONCURRENCY`` crawls run at once,
    protecting the host's RAM (the crawler throttles on memory too) and the
    browser fleet.
    """
    async with app.state.crawl_slots:
        job.status = "running"
        job.started_at = time.time()
        job.events.publish("status", job.to_dict())
        host = urlparse(job.url).netloc
        robots = await load_robots(job.url)
        # Link discovery checks robots.txt, but the seed URL was never checked.
        if not is_allowed(robots, job.url):
            job.status = "failed"
            job.errors.append(f"robots.txt disallows {job.url}")
            logger.info("Job %s: robots.txt disallows the seed %s", job.job_id, job.url)
            finish_job(job)
            return
        queue = RequestQueue()
        await queue.add(CrawlRequest(job.url, depth=0, max_depth=job.max_depth))

        # API intelligence: BrowserPool attaches the sniffer to every context,
        # so all page traffic is recorded as the crawl runs.
        sniffer = ProxySniffer() if API_INTELLIGENCE else None

        crawler = BaseCrawler(
            request_queue=queue,
            browser_pool=BrowserPool(headless=CRAWL_HEADLESS, api_sniffer=sniffer),
            session_pool=SessionPool(),
            concurrency=CRAWL_PAGE_CONCURRENCY,
            max_memory_percent=CRAWL_MAX_MEMORY_PERCENT,
            handler_timeout=PAGE_HANDLER_TIMEOUT,
        )

        async def handler(request: CrawlRequest, page: Any) -> None:
            entry = await process_page(job, request, page, queue, host, robots)
            if entry is not None:
                job.events.publish("page", {"page": page_summary(entry), "job": job.to_dict()})
            else:
                job.events.publish("status", job.to_dict())
            publish_new_errors(job)

        try:
            await crawler.run(handler)
            await crawler.wait_for_completion()
            if sniffer is not None:
                await publish_discovered_apis(job, sniffer)
            job.status = "succeeded"
            logger.info(
                "Job %s finished: %d pages, %d saved, %d duplicates, "
                "%d entities, %d relations, %d endpoints",
                job.job_id, job.pages_crawled, job.pages_saved,
                job.duplicates_skipped, job.entities_extracted,
                job.relations_written, job.endpoints_discovered,
            )
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.errors.append("cancelled by server shutdown")
            raise
        except Exception as exc:
            job.status = "failed"
            job.errors.append(f"crawl failed: {exc}")
            logger.exception("Job %s failed", job.job_id)
        finally:
            finish_job(job)


def page_summary(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The compact view of a page result sent in live events."""
    verified, unverified = structured.count_fields(entry.get("structured"))
    return {
        "url": entry.get("url"),
        "title": entry.get("title"),
        "status_code": entry.get("status_code"),
        "summary": entry.get("summary"),
        "entities": len(entry.get("entities") or []),
        "triples": len(entry.get("triples") or []),
        "fields_verified": verified,
        "fields_unverified": unverified,
    }


def publish_new_errors(job: Any) -> None:
    """Publishes each job error once, in order."""
    for message in job.errors[job.errors_published:]:
        job.events.publish("error", {"message": message})
    job.errors_published = len(job.errors)


def finish_job(job: Any) -> None:
    """Stamps the finish time and tells live subscribers the job is over."""
    job.finished_at = time.time()
    publish_new_errors(job)
    job.events.publish("done", job.to_dict())


# =============================================================================
# Application
# =============================================================================
@asynccontextmanager
async def lifespan(application: FastAPI):
    """Builds adapters on startup and releases them on shutdown.

    Connection failures are logged rather than raised, so the service still
    starts and /api/v1/health can report exactly which dependency is down.
    """
    application.state.cleaner = DataCleaner()
    application.state.embedder = DocumentEmbedder(embedding_client=None, chunk_size=CHUNK_SIZE)
    application.state.extractor = LLMExtractor()
    application.state.postgres = PostgresAdapter()
    application.state.qdrant = QdrantAdapter()
    application.state.neo4j = Neo4jAdapter()
    application.state.output_dir = _env_str("OUTPUT_DIR", "byconn_output")
    application.state.jobs: Dict[str, CrawlJob] = {}
    application.state.agent_jobs: Dict[str, AgentJob] = {}
    application.state.tasks: Set[asyncio.Task] = set()
    application.state.crawl_slots = asyncio.Semaphore(MAX_CRAWL_CONCURRENCY)

    async def _connect(name: str, adapter: Any) -> None:
        try:
            # Bounded: an unreachable backend must not stall startup.
            await asyncio.wait_for(adapter.connect(), timeout=STARTUP_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(
                "Connecting to %s timed out after %.1fs; continuing in degraded mode",
                name, STARTUP_CONNECT_TIMEOUT,
            )
        except Exception as exc:
            logger.error("Failed to connect to %s at startup: %s", name, exc)

    # Concurrent, so the worst-case wait is one timeout rather than three.
    await asyncio.gather(
        _connect("postgres", application.state.postgres),
        _connect("qdrant", application.state.qdrant),
        _connect("neo4j", application.state.neo4j),
    )

    if application.state.extractor.is_available:
        logger.info(
            "LLM extraction enabled (provider=%s, model=%s)",
            application.state.extractor.provider, application.state.extractor.model,
        )
    else:
        logger.warning("No LLM API key found; crawls will store pages without entities.")

    logger.info("BYCONN-X %s ready (max crawl concurrency=%d)", VERSION, MAX_CRAWL_CONCURRENCY)
    try:
        yield
    finally:
        for task in list(application.state.tasks):
            task.cancel()
        if application.state.tasks:
            await asyncio.gather(*application.state.tasks, return_exceptions=True)
        for name, adapter in (
            ("postgres", application.state.postgres),
            ("qdrant", application.state.qdrant),
            ("neo4j", application.state.neo4j),
        ):
            try:
                await adapter.close()
            except Exception as exc:
                logger.warning("Error closing %s adapter: %s", name, exc)
        logger.info("BYCONN-X shutdown complete.")


app = FastAPI(
    title="BYCONN-X Data Engine",
    description="AI-powered web data acquisition API.",
    version=VERSION,
    lifespan=lifespan,
)


@dataclass
class AgentJob:
    """In-memory record of one background visual-agent task."""

    job_id: str
    url: str
    task: str
    max_steps: int
    status: str = "queued"
    succeeded: bool = False
    steps_taken: int = 0
    endpoints_discovered: int = 0
    errors: List[str] = field(default_factory=list)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    # Each step's action and the planner's reason, as the agent took them.
    steps: List[Dict[str, Any]] = field(default_factory=list)
    events: EventHub = field(default_factory=EventHub, repr=False)
    errors_published: int = 0
    kind: str = "agent"

    def to_dict(self) -> Dict[str, Any]:
        """Returns a JSON-serialisable view of the agent job."""
        return {
            "kind": self.kind,
            "created_at": self.created_at,
            "steps": self.steps,
            "job_id": self.job_id,
            "url": self.url,
            "task": self.task,
            "max_steps": self.max_steps,
            "status": self.status,
            "succeeded": self.succeeded,
            "steps_taken": self.steps_taken,
            "endpoints_discovered": self.endpoints_discovered,
            "errors": self.errors[:20],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": (
                round(self.finished_at - self.started_at, 2)
                if self.started_at and self.finished_at
                else None
            ),
        }


async def run_agent_task(job: AgentJob) -> None:
    """Drives the visual browser agent against a target URL.

    Holds a crawl slot so agent tasks cannot exhaust the browser fleet.
    """
    async with app.state.crawl_slots:
        job.status = "running"
        job.started_at = time.time()
        job.events.publish("status", job.to_dict())
        sniffer = ProxySniffer() if API_INTELLIGENCE else None
        pool = BrowserPool(headless=CRAWL_HEADLESS, api_sniffer=sniffer)
        try:
            await pool.initialize()
            context = await pool.new_context()
            page = await context.new_page()
            try:
                await page.goto(job.url, wait_until="domcontentloaded")
                def on_step(record: Dict[str, Any]) -> None:
                    step = {"step": record.get("step"), "action": record.get("action")}
                    job.steps.append(step)
                    job.steps_taken = len(job.steps)
                    job.events.publish("step", step)

                agent = VisualBrowserAgent(page, llm_client=app.state.extractor, on_step=on_step)
                job.succeeded = await agent.execute_task(job.task, max_steps=job.max_steps)
                job.steps_taken = len(agent.history)
                if not job.succeeded and agent.failure_reason:
                    job.errors.append(f"agent did not reach the goal: {agent.failure_reason}")
            finally:
                await context.close()
            if sniffer is not None:
                job.endpoints_discovered = len(sniffer.get_summary())
                await publish_discovered_apis(_as_crawl_job(job.job_id), sniffer)
            job.status = "succeeded"
            logger.info("Agent job %s finished after %d steps (succeeded=%s).",
                        job.job_id, job.steps_taken, job.succeeded)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.errors.append("cancelled by server shutdown")
            raise
        except Exception as exc:
            job.status = "failed"
            job.errors.append(f"agent task failed: {exc}")
            logger.exception("Agent job %s failed", job.job_id)
        finally:
            try:
                await pool.close()
            except Exception as exc:
                logger.warning("Error closing agent browser pool: %s", exc)
            finish_job(job)


def _as_crawl_job(job_id: str) -> CrawlJob:
    """Adapter so agent runs can reuse the API-intelligence publisher."""
    return CrawlJob(job_id=job_id, url="", max_depth=0)


def _record(app_state: Any, job: CrawlJob) -> None:
    """Stores a job, trimming the oldest records once the cap is exceeded."""
    jobs: Dict[str, CrawlJob] = app_state.jobs
    jobs[job.job_id] = job
    while len(jobs) > MAX_JOBS_RETAINED:
        oldest = min(jobs.values(), key=lambda j: j.started_at or float("inf"))
        jobs.pop(oldest.job_id, None)


@app.post("/api/v1/crawl", response_model=CrawlAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_crawl(body: CrawlBody) -> CrawlAccepted:
    """Queues a crawl and returns immediately with a job id.

    The crawl itself runs in the background; poll ``/api/v1/crawl/{job_id}``
    for progress.
    """
    job = CrawlJob(
        job_id=uuid.uuid4().hex,
        url=body.url,
        max_depth=body.max_depth,
        prompt=body.prompt.strip(),
        schema=body.json_schema,
    )
    _record(app.state, job)

    task = asyncio.create_task(run_crawl_pipeline(job), name=f"crawl-{job.job_id}")
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)

    logger.info("Queued crawl %s for %s (max_depth=%d)", job.job_id, job.url, job.max_depth)
    return CrawlAccepted(
        job_id=job.job_id,
        status=job.status,
        url=job.url,
        max_depth=job.max_depth,
        pages_queued=1,
        prompt=job.prompt,
        has_schema=job.schema is not None,
    )


@app.get("/api/v1/crawl/{job_id}")
async def get_crawl(job_id: str) -> Dict[str, Any]:
    """Returns the current status and counters for a crawl job."""
    job = app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job id: {job_id}")
    return job.to_dict()


@app.post("/api/v1/act", response_model=ActionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_agent(body: ActionBody) -> ActionAccepted:
    """Queues a visual-agent task and returns immediately with a job id.

    The agent opens the URL and repeatedly plans and performs actions until the
    task is done or ``max_steps`` is reached. Poll ``/api/v1/act/{job_id}``.
    """
    job = AgentJob(
        job_id=uuid.uuid4().hex,
        url=body.url,
        task=body.task,
        max_steps=body.max_steps,
    )
    app.state.agent_jobs[job.job_id] = job
    while len(app.state.agent_jobs) > MAX_JOBS_RETAINED:
        oldest = min(app.state.agent_jobs.values(), key=lambda j: j.started_at or float("inf"))
        app.state.agent_jobs.pop(oldest.job_id, None)

    task = asyncio.create_task(run_agent_task(job), name=f"agent-{job.job_id}")
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)

    logger.info("Queued agent job %s for %s (task=%r)", job.job_id, job.url, job.task)
    return ActionAccepted(
        job_id=job.job_id,
        status=job.status,
        url=job.url,
        task=job.task,
    )


@app.get("/api/v1/act/{job_id}")
async def get_agent(job_id: str) -> Dict[str, Any]:
    """Returns the current status and step trace of an agent job."""
    job = app.state.agent_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent job id: {job_id}")
    return job.to_dict()


def _sse(event: str, data: Any) -> str:
    """Formats one server-sent event."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _job_event_stream(job: Any, request: Request):
    """Streams a job's events: a snapshot first, then live events until done.

    Subscribes before taking the snapshot, so nothing that happens in between
    is lost. An idle stream sends heartbeats and re-checks the job, so it ends
    even if its ``done`` event was dropped for a slow client.
    """
    queue = job.events.subscribe()
    try:
        yield _sse("snapshot", job.to_dict())
        if job.status in TERMINAL_STATUSES:
            yield _sse("done", job.to_dict())
            return
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                if job.status in TERMINAL_STATUSES and job.finished_at:
                    yield _sse("done", job.to_dict())
                    return
                if await request.is_disconnected():
                    return
                yield ": heartbeat\n\n"
                continue
            yield _sse(event, data)
            if event == "done":
                return
    finally:
        job.events.unsubscribe(queue)


def _event_response(job: Any, request: Request) -> StreamingResponse:
    return StreamingResponse(
        _job_event_stream(job, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/crawl/{job_id}/events")
async def crawl_events(job_id: str, request: Request) -> StreamingResponse:
    """Live progress of a crawl as server-sent events.

    Events: ``snapshot``, ``status``, ``page``, ``error`` and a final ``done``.
    """
    job = app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job id: {job_id}")
    return _event_response(job, request)


@app.get("/api/v1/act/{job_id}/events")
async def agent_events(job_id: str, request: Request) -> StreamingResponse:
    """Live progress of an agent task: ``snapshot``, ``status``, ``step``,
    ``error`` and a final ``done``."""
    job = app.state.agent_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent job id: {job_id}")
    return _event_response(job, request)


# =============================================================================
# Results, history and export
# =============================================================================
def build_graph(pages: List[Dict[str, Any]], cap: int = GRAPH_NODE_CAP) -> Dict[str, Any]:
    """Builds the knowledge graph from the entities and triples pages returned.

    Nodes are deduplicated by case-folded name and typed from the entity
    lists; every edge keeps the URL it was extracted from. At most ``cap``
    nodes are returned, with ``truncated`` set when more existed.
    """
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    seen_edges: Set[tuple] = set()
    truncated = False

    def node_id(name: Any, entity_type: Optional[str] = None) -> Optional[str]:
        nonlocal truncated
        label = str(name or "").strip()
        key = label.casefold()
        if not key:
            return None
        if key in nodes:
            if entity_type and nodes[key]["type"] == "ENTITY":
                nodes[key]["type"] = entity_type
            return nodes[key]["id"]
        if len(nodes) >= cap:
            truncated = True
            return None
        nodes[key] = {"id": f"n{len(nodes)}", "label": label[:200], "type": entity_type or "ENTITY"}
        return nodes[key]["id"]

    for page in pages:
        types: Dict[str, str] = {}
        for entity in page.get("entities") or []:
            if isinstance(entity, dict) and entity.get("name"):
                entity_type = str(entity.get("type") or entity.get("entity_type") or "ENTITY").upper()
                types[str(entity["name"]).strip().casefold()] = entity_type
                node_id(entity["name"], entity_type)
        for triple in page.get("triples") or []:
            if not isinstance(triple, dict):
                continue
            subject, obj = triple.get("subject"), triple.get("object")
            source = node_id(subject, types.get(str(subject or "").strip().casefold()))
            target = node_id(obj, types.get(str(obj or "").strip().casefold()))
            predicate = str(triple.get("predicate") or "").strip()
            if not (source and target and predicate):
                continue
            key = (source, target, predicate.upper())
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({
                "source": source, "target": target,
                "type": predicate, "source_url": page.get("url"),
            })
    return {"nodes": list(nodes.values()), "edges": edges, "truncated": truncated}


async def _results_for(job_id: str) -> Dict[str, Any]:
    """Collects a crawl's results from memory, or PostgreSQL once evicted."""
    job = app.state.jobs.get(job_id)
    if job is not None:
        meta = job.to_dict()
        pages = list(job.pages)
        merged = job.structured or structured.empty_structured()
    else:
        try:
            pages = await asyncio.wait_for(
                app.state.postgres.list_extraction_results(job_id),
                timeout=PIPELINE_STEP_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("Could not load archived results for %s: %s", job_id, exc)
            pages = []
        if not pages:
            raise HTTPException(status_code=404, detail=f"Unknown job id: {job_id}")
        # The job itself is gone from memory; only its stored pages remain.
        meta = {"kind": "crawl", "job_id": job_id, "status": "archived", "url": pages[0].get("url")}
        merged = structured.merge_structured(p.get("structured") for p in pages)
    return {"job": meta, "pages": pages, "structured": merged, "graph": build_graph(pages)}


@app.get("/api/v1/crawl/{job_id}/results")
async def crawl_results(job_id: str) -> Dict[str, Any]:
    """Everything a crawl produced: per-page results, the structured data with
    citations, and the knowledge graph built from the extracted relations."""
    return await _results_for(job_id)


@app.get("/api/v1/jobs")
async def list_jobs(limit: int = 50) -> Dict[str, Any]:
    """Lists recent crawl and agent jobs, newest first."""
    jobs = list(app.state.jobs.values()) + list(app.state.agent_jobs.values())
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    limit = max(1, min(limit, MAX_JOBS_RETAINED))
    return {"count": len(jobs), "jobs": [j.to_dict() for j in jobs[:limit]]}


def _csv_cell(value: Any) -> str:
    """Renders one CSV cell, neutralising spreadsheet formulas."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and text.startswith(CSV_FORMULA_PREFIXES):
        text = "'" + text
    return text


def structured_table(result: Dict[str, Any], field_name: str = "") -> Optional[tuple]:
    """Finds the array of objects in structured data that a CSV should hold.

    Returns ``(field, columns, rows)`` or None. Each row gains a ``sources``
    column listing the URLs its verified citations came from.
    """
    data = result.get("data") or {}
    if not isinstance(data, dict):
        return None
    candidates = [
        key for key, value in data.items()
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value)
    ]
    if field_name:
        candidates = [key for key in candidates if key == field_name]
    if not candidates:
        return None
    key = candidates[0]
    items = data[key]
    columns: List[str] = []
    for item in items:
        for column in item:
            if column not in columns:
                columns.append(column)
    citations = result.get("citations") or {}
    prefix = "/" + structured.escape_token(key) + "/"
    rows = []
    for index, item in enumerate(items):
        row = [_csv_cell(item.get(column)) for column in columns]
        sources = sorted({
            quote.get("url") for pointer, quotes in citations.items()
            if pointer.startswith(f"{prefix}{index}/") for quote in quotes if quote.get("url")
        })
        rows.append(row + [" ".join(sources)])
    return key, columns + ["sources"], rows


@app.get("/api/v1/crawl/{job_id}/export")
async def export_results(job_id: str, format: str = "json", field: str = "") -> Response:
    """Downloads a crawl's results as json, jsonl (one page per line) or csv
    (the array of objects in the structured data)."""
    results = await _results_for(job_id)
    stem = f"byconn-{job_id[:8]}"
    if format == "json":
        body, media, ext = json.dumps(results, indent=2, default=str), "application/json", "json"
    elif format == "jsonl":
        body = "".join(json.dumps(page, default=str) + "\n" for page in results["pages"])
        media, ext = "application/x-ndjson", "jsonl"
    elif format == "csv":
        table = structured_table(results["structured"], field)
        if table is None:
            raise HTTPException(
                status_code=400,
                detail="CSV needs structured data containing an array of objects; "
                       "run the crawl with a schema that has one, or export json.",
            )
        _key, columns, rows = table
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        writer.writerows(rows)
        body, media, ext = buffer.getvalue(), "text/csv", "csv"
    else:
        raise HTTPException(status_code=400, detail="format must be json, jsonl or csv")
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'},
    )


@app.get("/api/v1/apis")
async def list_discovered_apis(host: str = "", limit: int = 100) -> Dict[str, Any]:
    """Lists REST endpoints sniffed from previous crawls."""
    rows = await guarded_step(
        None, host or "-", "list discovered apis",
        app.state.postgres.list_discovered_apis(host=host, limit=limit),
    )
    return {"count": len(rows or []), "endpoints": rows or []}


@app.get("/api/v1/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    """Reports per-dependency health.

    Returns 200 when every dependency is reachable and 503 when any is down, so
    orchestrators can gate traffic on it.
    """
    checks = (
        ("postgres", app.state.postgres),
        ("qdrant", app.state.qdrant),
        ("neo4j", app.state.neo4j),
    )
    results = await asyncio.gather(
        *(
            asyncio.wait_for(adapter.ping(), timeout=HEALTH_PROBE_TIMEOUT)
            for _, adapter in checks
        ),
        return_exceptions=True,
    )

    components: List[ComponentStatus] = []
    for (name, _adapter), outcome in zip(checks, results, strict=True):
        if isinstance(outcome, BaseException):
            components.append(ComponentStatus(name=name, status="down", detail=str(outcome)))
        elif outcome:
            components.append(ComponentStatus(name=name, status="up"))
        else:
            components.append(
                ComponentStatus(name=name, status="down", detail="not reachable")
            )

    extractor = app.state.extractor
    llm = ComponentStatus(
        name="llm",
        status="up" if extractor.is_available else "disabled",
        # Provider and model only; the credential never leaves the server.
        detail=(
            f"{extractor.provider} · {extractor.model}"
            if extractor.is_available else "no API key configured"
        ),
    )
    embedder = app.state.embedder
    embeddings = ComponentStatus(
        name="embeddings",
        status="up" if embedder.is_available else "disabled",
        detail=None if embedder.is_available else (
            "no embedding provider; vector indexing is skipped"
        ),
    )

    healthy = all(c.status == "up" for c in components)
    response.status_code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    active = sum(
        1 for j in app.state.jobs.values() if j.status in {"queued", "running"}
    ) + sum(
        1 for j in app.state.agent_jobs.values() if j.status in {"queued", "running"}
    )
    return HealthResponse(
        status="ok" if healthy else "degraded",
        version=VERSION,
        uptime_seconds=round(time.time() - STARTED_AT, 2),
        llm=llm,
        embeddings=embeddings,
        components=components,
        active_jobs=active,
    )


# Orchestrators, container healthchecks and uptime monitors conventionally probe
# ``/health`` rather than a versioned API path. Alias it so a standard probe
# does not get a 404 and misreport the deployment as down.
app.add_api_route(
    "/health",
    health,
    methods=["GET"],
    response_model=HealthResponse,
    summary="Alias of /api/v1/health for standard liveness probes",
)


if DASHBOARD_DIR.is_dir():
    app.mount("/dashboard", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")
    logger.info("Dashboard mounted at /dashboard (from %s)", DASHBOARD_DIR)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        """Redirects the root path to the operator dashboard."""
        return RedirectResponse(url="/dashboard/index.html")
else:  # pragma: no cover - depends on checkout
    logger.warning("Dashboard directory not found at %s; /dashboard not mounted", DASHBOARD_DIR)


def main() -> None:
    """Console-script entry point (`byconn-server`).

    Equivalent to `uvicorn server:app`, configured from HOST/PORT/RELOAD.
    """
    import uvicorn

    uvicorn.run(
        "server:app",
        host=_env_str_or("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT") or 8000),
        reload=_env_bool("RELOAD", False),
        log_level=_env_str_or("LOG_LEVEL", "info").lower(),
    )


def _env_str_or(key: str, default: str) -> str:
    """Reads a string env var, treating blank values as unset."""
    value = os.getenv(key)
    return value if value else default


if __name__ == "__main__":  # pragma: no cover
    main()
