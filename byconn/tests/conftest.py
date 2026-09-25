"""Pytest fixtures for the BYCONN-X suite.

Fixtures only: the reusable test doubles and helpers live in
:mod:`byconn.tests.doubles`, which test modules import directly.
"""

import pytest

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
