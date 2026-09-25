"""Test doubles and helpers shared across the BYCONN-X suite.

Kept out of ``conftest.py`` on purpose: test modules import from here, while
``conftest.py`` only holds pytest fixtures. Importing a conftest directly is
fragile once a directory becomes a package.
"""

import asyncio
import time
from typing import Any, Dict, List

# --------------------------------------------------------------------------
# event-loop helper
# --------------------------------------------------------------------------
# The suite avoids a pytest-asyncio dependency; async code under test is driven
# through asyncio.run() so plain sync tests stay simple.


def run_async(coro):
    """Runs a coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# fake browser / crawler
# --------------------------------------------------------------------------
HTML_PAGES: Dict[str, tuple] = {
    "https://example.com/": (
        "Home",
        """<html><head><title>Home</title><style>.a{color:red}</style>
        <script>var evil=1;</script></head><body><h1>Home</h1>
        <p>Welcome to Example Corp.</p>
        <a href="/a">Alpha</a> <a href="/b">Beta</a>
        <a href="https://other.com/x">External</a>
        <a href="#frag">Anchor</a> <a href="mailto:a@b.c">Mail</a>
        <a href="javascript:void(0)">JS</a>
        <a href="/a">Alpha again</a></body></html>""",
    ),
    "https://example.com/a": (
        "Alpha",
        '<html><body><h1>Alpha</h1><p>Alpha body.</p>'
        '<a href="/c">Gamma</a><a href="/">Home</a></body></html>',
    ),
    "https://example.com/b": (
        "Beta",
        "<html><body><h1>Beta</h1><p>Beta body.</p></body></html>",
    ),
    "https://example.com/c": (
        "Gamma",
        "<html><body><h1>Gamma</h1><p>Gamma body.</p></body></html>",
    ),
}


class FakePage:
    """Stands in for a Playwright Page."""

    def __init__(self, url: str):
        self.url = url

    async def title(self) -> str:
        return HTML_PAGES.get(self.url, ("", ""))[0]

    async def content(self) -> str:
        return HTML_PAGES.get(self.url, ("", ""))[1]


class FakeBrowserPool:
    """Stands in for BrowserPool without launching Chromium."""

    instances: List["FakeBrowserPool"] = []

    def __init__(self, headless: bool = True, **kwargs):
        self.headless = headless
        self.closed = False
        FakeBrowserPool.instances.append(self)

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeCrawler:
    """Drains the real RequestQueue and invokes the handler, like BaseCrawler."""

    def __init__(self, request_queue=None, browser_pool=None, session_pool=None,
                 concurrency: int = 1, max_memory_percent: float = 90.0, **kwargs):
        self.request_queue = request_queue
        self.concurrency = concurrency
        self._handler = None

    async def run(self, handler) -> None:
        self._handler = handler

    async def wait_for_completion(self) -> None:
        guard = 0
        while not await self.request_queue.is_finished():
            guard += 1
            if guard > 500:
                raise RuntimeError("FakeCrawler failed to drain the queue")
            request = await self.request_queue.get_next()
            if request is None:
                await asyncio.sleep(0.005)
                continue
            try:
                await self._handler(request, FakePage(request.url))
                await self.request_queue.complete(request)
            except Exception:
                await self.request_queue.fail(request)

    async def stop(self) -> None:
        return None


# --------------------------------------------------------------------------
# database doubles
# --------------------------------------------------------------------------
class FakePostgres:
    """Records every page and entity the pipeline writes."""

    def __init__(self, **kwargs):
        self.pages: List[Dict[str, Any]] = []
        self.entities: List[tuple] = []
        self.discovered: List[Dict[str, Any]] = []
        self.health: List[Dict[str, Any]] = []
        self.connected = False
        self.fail = False

    async def connect(self) -> "FakePostgres":
        self.connected = True
        return self

    async def close(self) -> None:
        self.connected = False

    async def ping(self) -> bool:
        return self.connected

    async def upsert_crawled_page(self, url: str, markdown: str = "", title: str = "",
                                 job_id: str = "", chunk_count: int = 0,
                                 status_code: int = 200, depth: int = 0, **kwargs) -> int:
        if self.fail:
            raise ConnectionRefusedError("postgres down")
        self.pages.append(dict(url=url, markdown=markdown, title=title, job_id=job_id,
                               chunk_count=chunk_count, depth=depth))
        return 1000 + len(self.pages)

    async def insert_entities(self, page_id, entities, source_url: str = "") -> int:
        if self.fail:
            raise ConnectionRefusedError("postgres down")
        self.entities.append((page_id, list(entities or []), source_url))
        return len(entities or [])

    async def register_discovered_api(self, endpoint: Dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionRefusedError("postgres down")
        self.discovered.append(dict(endpoint))

    async def register_discovered_apis(self, endpoints) -> int:
        written = 0
        for endpoint in endpoints or []:
            if not isinstance(endpoint, dict) or not endpoint.get("url"):
                continue
            await self.register_discovered_api(endpoint)
            written += 1
        return written

    async def list_discovered_apis(self, host: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.discovered
        if host:
            rows = [r for r in rows if r.get("host") == host]
        return [dict(r) for r in rows[:limit]]

    async def record_api_health(self, api_id: str, url: str, status_code: int = 0,
                                latency_ms: int = 0, is_up: bool = False,
                                error_message: str = "", api_name: str = "") -> None:
        self.health.append(dict(api_id=api_id, url=url, status_code=status_code,
                                latency_ms=latency_ms, is_up=is_up,
                                error_message=error_message, api_name=api_name))


class FakeQdrant:
    """Records chunk upserts."""

    def __init__(self, **kwargs):
        self.upserts: List[Dict[str, Any]] = []
        self.connected = False
        self.fail = False

    async def connect(self) -> "FakeQdrant":
        self.connected = True
        return self

    async def close(self) -> None:
        self.connected = False

    async def ping(self) -> bool:
        return self.connected

    async def upsert_embeddings(self, url: str, chunks, embeddings, job_id: str = "",
                                payload_extra=None, **kwargs) -> int:
        if self.fail:
            raise ConnectionRefusedError("qdrant down")
        self.upserts.append(dict(url=url, n=len(chunks), job_id=job_id))
        return len(chunks)


class FakeNeo4j:
    """Records graph writes."""

    def __init__(self, **kwargs):
        self.pages: List[str] = []
        self.links: List[tuple] = []
        self.triples: List[tuple] = []
        self.connected = False

    async def connect(self) -> "FakeNeo4j":
        self.connected = True
        return self

    async def close(self) -> None:
        self.connected = False

    async def ping(self) -> bool:
        return self.connected

    async def upsert_page(self, url: str, **kwargs) -> None:
        self.pages.append(url)

    async def link_page_entities(self, url: str, entities) -> int:
        self.links.append((url, list(entities or [])))
        return len(entities or [])

    async def write_triples(self, triples, properties=None, strict: bool = False) -> int:
        self.triples.append((list(triples or []), properties))
        return len(triples or [])


class FakeExtractor:
    """LLMExtractor stand-in returning a canned, valid knowledge graph."""

    CANNED = {
        "entities": [
            {"name": "Example Corp", "type": "ORGANIZATION"},
            {"name": "Alice", "type": "PERSON"},
        ],
        "triples": [{"subject": "Example Corp", "predicate": "EMPLOYS", "object": "Alice"}],
        "topics": ["example"],
        "summary": "An example page.",
    }

    instances: List["FakeExtractor"] = []

    def __init__(self, *args, **kwargs):
        self.provider = "openai"
        self.model = "fake-model"
        self.available = True
        self.seen_text: List[str] = []
        FakeExtractor.instances.append(self)

    @property
    def is_available(self) -> bool:
        return self.available

    @staticmethod
    def empty_result() -> Dict[str, Any]:
        return {"entities": [], "triples": [], "topics": [], "summary": ""}

    async def extract_knowledge(self, text: str) -> Dict[str, Any]:
        self.seen_text.append(text)
        return dict(self.CANNED)


class FakeEmbedder:
    """DocumentEmbedder stand-in producing deterministic 384-dim vectors."""

    def __init__(self, *args, **kwargs):
        self.chunk_size = 500
        self.chunk_overlap = 50
        self.dimensions = 384
        self.chunks: List[str] = []

    @property
    def is_available(self) -> bool:
        return True

    def split_into_chunks(self, text: str) -> List[str]:
        words = (text or "").split()
        return [" ".join(words[i:i + 20]) for i in range(0, len(words), 20)]

    async def generate_dense_embeddings(self, chunks):
        return [[0.5] * 384 for _ in chunks]


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def poll_job(client, job_id: str, timeout: float = 20.0) -> Dict[str, Any]:
    """Polls a crawl job until it reaches a terminal state."""
    deadline = time.time() + timeout
    payload: Dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/crawl/{job_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished; last payload={payload}")


def start_crawl(client, url: str, max_depth: int = 0) -> str:
    """Queues a crawl and returns the job id."""
    response = client.post("/api/v1/crawl", json={"url": url, "max_depth": max_depth})
    assert response.status_code == 202, response.text
    return response.json()["job_id"]
