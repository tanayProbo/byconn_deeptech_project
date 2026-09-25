"""Pytest fixtures for the BYCONN-X suite.

Fixtures only: the reusable test doubles and helpers live in
:mod:`byconn.tests.doubles`, which test modules import directly.

**Hermeticity.** The suite must behave identically on every machine, so a
developer's real ``.env`` is neutralised before anything is imported. Both
:mod:`server` and :mod:`byconn.storage.adapters` call ``load_dotenv()`` at
import time, and ``server`` can be imported part-way through a run, which would
otherwise re-inject credentials and make tests hit a real LLM or database.
"""

import os

import pytest

# Environment variables that steer providers, endpoints and timeouts.
_MANAGED_ENV = (
    "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL", "GROQ_API_KEY",
    "MODEL_NAME", "VISION_MODEL_NAME", "EMBEDDING_PROVIDER", "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS", "LLM_PROVIDER", "LLM_TEMPERATURE", "LLM_MAX_RETRIES",
    "LLM_MAX_INPUT_TOKENS", "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT",
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "QDRANT_URL",
    "QDRANT_HOST", "QDRANT_PORT", "QDRANT_HTTPS", "QDRANT_API_KEY",
    "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_DATABASE",
    "HOST", "PORT", "RELOAD", "LOG_LEVEL", "OUTPUT_DIR", "STARTUP_CONNECT_TIMEOUT",
    "PIPELINE_STEP_TIMEOUT", "HEALTH_PROBE_TIMEOUT", "CRAWL_CONCURRENCY",
    "CRAWL_PAGE_CONCURRENCY", "CRAWL_MAX_PAGES", "CRAWL_HEADLESS",
    "CRAWL_DEDUPLICATE", "CRAWL_FOLLOW_LINKS", "API_INTELLIGENCE",
)

# Runs at conftest import, i.e. before any test module is imported.
for _key in _MANAGED_ENV:
    os.environ.pop(_key, None)

# Stop any module-level load_dotenv() from re-injecting them mid-run.
try:  # pragma: no cover - depends on dotenv being installed
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False
except ImportError:  # pragma: no cover
    pass

from byconn.tests.doubles import (
    FakeBrowserPool,
    FakeCrawler,
    FakeEmbedder,
    FakeExtractor,
    FakeNeo4j,
    FakePostgres,
    FakeQdrant,
)


@pytest.fixture
def fake_embedder():
    """A DocumentEmbedder double with no real embedding provider."""
    return FakeEmbedder()


@pytest.fixture
def server_app(monkeypatch, fake_embedder):
    """The FastAPI app with every external dependency replaced by a double."""
    import server as server_module

    monkeypatch.setattr(server_module, "PostgresAdapter", FakePostgres)
    monkeypatch.setattr(server_module, "QdrantAdapter", FakeQdrant)
    monkeypatch.setattr(server_module, "Neo4jAdapter", FakeNeo4j)
    monkeypatch.setattr(server_module, "LLMExtractor", FakeExtractor)
    monkeypatch.setattr(server_module, "DocumentEmbedder", lambda *a, **k: fake_embedder)
    monkeypatch.setattr(server_module, "BrowserPool", FakeBrowserPool)
    monkeypatch.setattr(server_module, "BaseCrawler", FakeCrawler)

    async def _no_robots(base_url: str):
        return None

    monkeypatch.setattr(server_module, "load_robots", _no_robots)

    pytest.importorskip("fastapi")
    starlette_testclient = pytest.importorskip("fastapi.testclient")
    return server_module.app, starlette_testclient.TestClient


@pytest.fixture
def client(server_app):
    """A TestClient with the application lifespan running."""
    app, TestClient = server_app
    with TestClient(app) as test_client:
        yield test_client
