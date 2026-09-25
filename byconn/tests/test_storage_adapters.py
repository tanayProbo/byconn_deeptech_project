"""Tests for byconn.storage.adapters.

Covers the pure helpers directly, the PostgreSQL and Neo4j contracts with
instrumented doubles, and the Qdrant adapter end-to-end against qdrant-client's
embedded engine (no server required).
"""

import asyncio

import pytest

from byconn.storage.adapters import (
    DEFAULT_COLLECTION,
    DEFAULT_NEO4J_PASSWORD,
    DEFAULT_NEO4J_URI,
    DEFAULT_PG_DSN,
    DEFAULT_QDRANT_URL,
    DEFAULT_VECTOR_SIZE,
    BaseAdapter,
    Neo4jAdapter,
    PostgresAdapter,
    QdrantAdapter,
    _env_bool,
    _env_float,
    _env_int,
    _env_str,
    _postgres_dsn,
    _qdrant_url,
    _redact,
    _safe_identifier,
    url_fingerprint,
)

from byconn.tests.doubles import run_async


# ==========================================================================
# environment helpers
# ==========================================================================
class TestEnvHelpers:
    def test_str_falls_back_when_blank(self, monkeypatch):
        monkeypatch.setenv("BYCONN_T", "")
        assert _env_str("BYCONN_T", "fallback") == "fallback"

    def test_str_returns_value(self, monkeypatch):
        monkeypatch.setenv("BYCONN_T", "value")
        assert _env_str("BYCONN_T", "fallback") == "value"

    def test_int_falls_back_on_garbage(self, monkeypatch):
        monkeypatch.setenv("BYCONN_I", "abc")
        assert _env_int("BYCONN_I", 7) == 7

    def test_int_accepts_zero(self, monkeypatch):
        # The adapter helper permits 0, unlike the pipeline helper which
        # treats it as unset because its values are counts.
        monkeypatch.setenv("BYCONN_I", "0")
        assert _env_int("BYCONN_I", 7) == 0

    def test_int_parses_value(self, monkeypatch):
        monkeypatch.setenv("BYCONN_I", "12")
        assert _env_int("BYCONN_I", 7) == 12

    def test_float_falls_back_on_garbage(self, monkeypatch):
        monkeypatch.setenv("BYCONN_F", "x")
        assert _env_float("BYCONN_F", 1.5) == 1.5

    def test_bool_truthy_spellings(self, monkeypatch):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("BYCONN_B", raw)
            assert _env_bool("BYCONN_B", False) is True, raw

    def test_bool_falsy_spellings(self, monkeypatch):
        for raw in ("0", "false", "no", "off"):
            monkeypatch.setenv("BYCONN_B", raw)
            assert _env_bool("BYCONN_B", True) is False, raw


class TestEndpointResolution:
    def test_database_url_wins(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:6543/db")
        assert _postgres_dsn() == "postgresql://u:p@h:6543/db"

    def test_postgres_parts_are_composed(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("POSTGRES_HOST", "pg")
        monkeypatch.setenv("POSTGRES_PORT", "6000")
        monkeypatch.setenv("POSTGRES_USER", "pu")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pp")
        monkeypatch.setenv("POSTGRES_DB", "pdb")
        assert _postgres_dsn() == "postgresql://pu:pp@pg:6000/pdb"

    def test_pg_default(self, monkeypatch):
        for key in ("DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT",
                    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            monkeypatch.delenv(key, raising=False)
        assert _postgres_dsn() == DEFAULT_PG_DSN

    def test_qdrant_url_wins(self, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://q.test:6333")
        assert _qdrant_url() == "http://q.test:6333"

    def test_qdrant_parts_are_composed(self, monkeypatch):
        monkeypatch.delenv("QDRANT_URL", raising=False)
        monkeypatch.setenv("QDRANT_HOST", "q")
        monkeypatch.setenv("QDRANT_PORT", "7000")
        monkeypatch.setenv("QDRANT_HTTPS", "true")
        assert _qdrant_url() == "https://q:7000"

    def test_qdrant_default(self, monkeypatch):
        for key in ("QDRANT_URL", "QDRANT_HOST", "QDRANT_PORT", "QDRANT_HTTPS"):
            monkeypatch.delenv(key, raising=False)
        assert _qdrant_url() == DEFAULT_QDRANT_URL

    def test_redact_masks_password(self):
        assert _redact("postgresql://u:hunter2@h/db") == "postgresql://u:***@h/db"

    def test_redact_leaves_credential_free_dsn(self):
        assert _redact("postgresql://localhost/db") == "postgresql://localhost/db"


# ==========================================================================
# helpers
# ==========================================================================
class TestHelpers:
    def test_url_fingerprint_is_stable(self):
        assert url_fingerprint("https://a.test/") == url_fingerprint("https://a.test/")

    def test_url_fingerprint_differs_per_url(self):
        assert url_fingerprint("https://a.test/") != url_fingerprint("https://b.test/")

    def test_qdrant_point_id_is_deterministic(self):
        first = QdrantAdapter.point_id("https://a.test/", 3)
        second = QdrantAdapter.point_id("https://a.test/", 3)
        assert first == second
        assert len(first) == 36  # UUID string

    def test_qdrant_point_id_varies_by_chunk(self):
        assert QdrantAdapter.point_id("https://a.test/", 0) != QdrantAdapter.point_id("https://a.test/", 1)

    def test_qdrant_point_id_varies_by_url(self):
        assert QdrantAdapter.point_id("https://a.test/", 0) != QdrantAdapter.point_id("https://b.test/", 0)


class TestCypherIdentifierAllowlist:
    @pytest.mark.parametrize("bad", [
        "Entity;DROP DATABASE neo4j",
        "n) RETURN 1 //",
        "1abc",
        "a-b",
        "",
        "x" * 80,
    ])
    def test_rejects_unsafe_identifiers(self, bad):
        with pytest.raises(ValueError):
            _safe_identifier(bad, "label")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            _safe_identifier(123, "label")

    def test_accepts_valid_label(self):
        assert _safe_identifier("Entity", "label") == "Entity"

    def test_predicate_is_normalised_to_a_relationship_type(self):
        assert _safe_identifier("developed by", "relationship type") == "DEVELOPED_BY"

    def test_relationship_type_is_uppercased(self):
        assert _safe_identifier("employs", "relationship type") == "EMPLOYS"


# ==========================================================================
# BaseAdapter contract
# ==========================================================================
class _Exploding(BaseAdapter):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.closed = False

    async def _connect(self):
        return None

    async def _close(self):
        self.closed = True

    async def _ping(self):
        raise self.error


class TestNeo4jPropertyEncoding:
    """Regression: property maps were passed as values, which Neo4j rejects.

    ``SET p.metadata = $metadata`` fails at runtime with "Property values can
    only be of primitive types or arrays thereof. Encountered: Map{}". The
    mapping has to be spread into individual, alias-qualified properties. This
    path never ran in CI because the Neo4j double accepted anything.
    """

    def test_spreads_a_map_into_individual_properties(self):
        from byconn.storage.adapters import _neo4j_properties

        clause, params = _neo4j_properties(
            {"source_url": "http://x.test/", "weight": 0.5}, alias="r"
        )
        assert clause == "r.source_url = $source_url, r.weight = $weight"
        assert params == {"source_url": "http://x.test/", "weight": 0.5}

    def test_qualifies_with_the_node_alias(self):
        from byconn.storage.adapters import _neo4j_properties

        clause, _ = _neo4j_properties({"depth": 0}, alias="p")
        assert clause == "p.depth = $depth", "an unqualified clause is a Cypher syntax error"

    def test_keeps_primitives_and_arrays_of_primitives(self):
        from byconn.storage.adapters import _neo4j_properties

        _clause, params = _neo4j_properties(
            {"s": "x", "i": 1, "f": 1.5, "b": True, "arr": ["a", "b"]}, alias="p"
        )
        assert params == {"s": "x", "i": 1, "f": 1.5, "b": True, "arr": ["a", "b"]}

    def test_json_encodes_values_neo4j_cannot_store(self):
        """A nested map must be preserved as text, not dropped or passed raw."""
        import json as json_module

        from byconn.storage.adapters import _neo4j_properties

        _clause, params = _neo4j_properties({"meta": {"x": 1}}, alias="p")
        assert json_module.loads(params["meta"]) == {"x": 1}
        assert isinstance(params["meta"], str), "a dict would be rejected by Neo4j"

    def test_skips_none_and_invalid_keys(self):
        from byconn.storage.adapters import _neo4j_properties

        _clause, params = _neo4j_properties(
            {"ok": 1, "none": None, "bad key!": 2, "'; DROP": 3}, alias="p"
        )
        assert params == {"ok": 1}

    def test_empty_mapping_produces_no_clause(self):
        from byconn.storage.adapters import _neo4j_properties

        assert _neo4j_properties(None) == ("", {})
        assert _neo4j_properties({}) == ("", {})

    def test_no_query_passes_a_map_as_a_property_value(self):
        """The generated Cypher must never bind a mapping to a property.

        Asserted against the real queries the adapter emits, not the source
        text, so a refactor cannot smuggle the old pattern back in.
        """
        from byconn.storage.adapters import Neo4jAdapter

        sink = {"cypher": []}
        adapter = Neo4jAdapter()
        adapter._driver = _FakeDriver(sink)
        adapter._connected = True

        run_async(adapter.upsert_page("http://x.test/", title="T", job_id="j",
                                      metadata={"depth": 0, "meta": {"a": 1}}))
        run_async(adapter.write_triples(
            [{"subject": "A", "predicate": "KNOWS", "object": "B"}],
            properties={"source_url": "http://x.test/", "nested": {"b": 2}},
        ))

        assert sink["cypher"], "expected the adapter to emit queries"
        for query, params in sink["cypher"]:
            assert "p.metadata = " not in query
            assert "r.properties = " not in query
            # Every bound value must be something Neo4j can store.
            for key, value in params.items():
                assert not isinstance(value, dict), (
                    f"parameter ${key} is a map; Neo4j rejects map property values"
                )
        # And the spread form is actually present.
        queries = [q for q, _ in sink["cypher"]]
        assert any("p.depth = $depth" in q for q in queries)
        assert any("r.source_url = $source_url" in q for q in queries)


class TestBaseAdapter:
    def test_starts_disconnected(self):
        assert _Exploding(RuntimeError()).is_connected is False

    def test_connect_is_idempotent(self):
        adapter = _Exploding(RuntimeError())
        run_async(adapter.connect())
        run_async(adapter.connect())
        assert adapter.is_connected is True

    def test_concurrent_connects_build_one_client(self):
        import asyncio

        class _Counting(_Exploding):
            def __init__(self):
                super().__init__(RuntimeError())
                self.connects = 0

            async def _connect(self):
                self.connects += 1
                await asyncio.sleep(0.01)

        async def scenario():
            adapter = _Counting()
            await asyncio.gather(*(adapter.connect() for _ in range(8)))
            return adapter

        adapter = run_async(scenario())
        assert adapter.connects == 1

    def test_ping_swallows_backend_errors(self):
        adapter = _Exploding(RuntimeError("connection refused"))
        assert run_async(adapter.ping()) is False

    def test_close_is_safe_before_connect(self):
        adapter = _Exploding(RuntimeError())
        run_async(adapter.close())
        assert adapter.closed is False

    def test_close_is_idempotent(self):
        adapter = _Exploding(RuntimeError())
        run_async(adapter.connect())
        run_async(adapter.close())
        run_async(adapter.close())
        assert adapter.closed is True
        assert adapter.is_connected is False

    def test_async_context_manager(self):
        async def scenario():
            async with _Exploding(RuntimeError()) as adapter:
                assert adapter.is_connected is True
            return adapter

        adapter = run_async(scenario())
        assert adapter.is_connected is False

    def test_unconnected_accessor_raises(self):
        adapter = PostgresAdapter(dsn="postgresql://u:p@h/db")
        with pytest.raises(RuntimeError, match="not initialized"):
            adapter._require_pool()

    def test_unconnected_qdrant_accessor_raises(self):
        adapter = QdrantAdapter(url="http://q.test:6333")
        with pytest.raises(RuntimeError, match="not initialized"):
            adapter._require_client()

    def test_unconnected_neo4j_accessor_raises(self):
        adapter = Neo4jAdapter()
        with pytest.raises(RuntimeError, match="not initialized"):
            adapter._require_driver()


# ==========================================================================
# Qdrant end-to-end (embedded engine, no server)
# ==========================================================================
@pytest.fixture
def qdrant(tmp_path):
    """A QdrantAdapter backed by qdrant-client's on-disk embedded engine.

    The private ``_client`` is replaced because the adapter targets a remote
    server while the embedded engine is selected at construction time.
    """
    from qdrant_client import AsyncQdrantClient

    adapter = QdrantAdapter(url="http://unused", collection_name=DEFAULT_COLLECTION,
                            vector_size=DEFAULT_VECTOR_SIZE)
    adapter._client = AsyncQdrantClient(path=str(tmp_path / "qdrant"))
    adapter._connected = True
    yield adapter
    run_async(adapter.close())


def _vector(seed: float = 0.5) -> list:
    return [seed] * DEFAULT_VECTOR_SIZE


class TestQdrantConnect:
    """Regression: connect() used to deadlock against a real Qdrant.

    ``_connect`` ran while ``BaseAdapter.connect`` held the adapter's
    ``asyncio.Lock``, and it called the public ``ensure_collection``, which
    called ``_ensure`` -> ``connect`` again. ``asyncio.Lock`` is not reentrant,
    so the second acquire waited for a lock the first call was still holding and
    the coroutine hung forever. The other suites missed it because their
    fixtures inject a client and set ``_connected``, skipping ``connect()``.

    These tests go through the real ``connect()`` path, wrapped in a timeout so
    a regression fails instead of hanging the run.
    """

    def _embedded(self, tmp_path, monkeypatch):
        from qdrant_client import AsyncQdrantClient

        import byconn.storage.adapters as adapters_module

        client = AsyncQdrantClient(path=str(tmp_path / "qdrant-connect"))
        monkeypatch.setattr(adapters_module, "AsyncQdrantClient", lambda **_: client)
        adapter = adapters_module.QdrantAdapter(
            url="http://unused", collection_name=DEFAULT_COLLECTION,
            vector_size=DEFAULT_VECTOR_SIZE,
        )
        return adapter, client

    def test_connect_does_not_deadlock(self, tmp_path, monkeypatch):
        adapter, client = self._embedded(tmp_path, monkeypatch)
        try:
            run_async(asyncio.wait_for(adapter.connect(), timeout=20))
            assert adapter.is_connected is True
        finally:
            run_async(client.close())

    def test_connect_creates_the_collection(self, tmp_path, monkeypatch):
        adapter, client = self._embedded(tmp_path, monkeypatch)
        try:
            run_async(asyncio.wait_for(adapter.connect(), timeout=20))
            assert run_async(adapter._require_client().collection_exists(
                DEFAULT_COLLECTION)) is True
        finally:
            run_async(client.close())

    def test_second_connect_is_a_no_op(self, tmp_path, monkeypatch):
        adapter, client = self._embedded(tmp_path, monkeypatch)
        try:
            run_async(asyncio.wait_for(adapter.connect(), timeout=20))
            run_async(asyncio.wait_for(adapter.connect(), timeout=20))
            assert adapter.is_connected is True
        finally:
            run_async(client.close())


class TestQdrantEmbedded:
    def test_creates_collection_with_384_cosine(self, qdrant):
        run_async(qdrant.ensure_collection())
        info = run_async(qdrant._require_client().get_collection(DEFAULT_COLLECTION))
        assert info.config.params.vectors.size == 384
        assert str(info.config.params.vectors.distance).upper() == "COSINE"

    def test_ensure_collection_is_idempotent(self, qdrant):
        run_async(qdrant.ensure_collection())
        run_async(qdrant.ensure_collection())
        assert run_async(qdrant.count_points()) == 0

    def test_ping_is_true(self, qdrant):
        assert run_async(qdrant.ping()) is True

    def test_upsert_and_count(self, qdrant):
        run_async(qdrant.ensure_collection())
        written = run_async(
            qdrant.upsert_embeddings("https://a.test/", ["c0", "c1"],
                                     [_vector(0.1), _vector(0.2)], job_id="j1")
        )
        assert written == 2
        assert run_async(qdrant.count_points()) == 2
        assert run_async(qdrant.count_points("https://a.test/")) == 2

    def test_reupsert_overwrites_instead_of_duplicating(self, qdrant):
        run_async(qdrant.ensure_collection())
        args = ("https://a.test/", ["c0"], [_vector(0.1)],)
        run_async(qdrant.upsert_embeddings(*args))
        run_async(qdrant.upsert_embeddings(*args))
        assert run_async(qdrant.count_points()) == 1

    def test_payload_round_trips(self, qdrant):
        run_async(qdrant.ensure_collection())
        run_async(qdrant.upsert_embeddings("https://a.test/", ["hello"],
                                           [_vector(0.3)], job_id="j9",
                                           payload_extra={"title": "T"}))
        point_id = QdrantAdapter.point_id("https://a.test/", 0)
        stored = run_async(qdrant._require_client().retrieve(DEFAULT_COLLECTION, ids=[point_id]))
        assert len(stored) == 1
        assert stored[0].payload["chunk"] == "hello"
        assert stored[0].payload["job_id"] == "j9"
        assert stored[0].payload["title"] == "T"

    def test_batched_upsert(self, qdrant):
        run_async(qdrant.ensure_collection())
        chunks = [f"c{i}" for i in range(150)]
        assert run_async(qdrant.upsert_embeddings("https://a.test/", chunks,
                                                  [_vector() for _ in chunks],
                                                  batch_size=64)) == 150
        assert run_async(qdrant.count_points()) == 150

    def test_search_returns_filtered_hits(self, qdrant):
        run_async(qdrant.ensure_collection())
        run_async(qdrant.upsert_embeddings("https://a.test/", ["alpha", "beta"],
                                           [_vector(0.1), _vector(0.9)], job_id="j1"))
        hits = run_async(qdrant.search(_vector(0.1), limit=2, url_filter="https://a.test/"))
        assert len(hits) == 2
        assert all(isinstance(hit["score"], float) for hit in hits)
        assert all(hit["payload"]["url"] == "https://a.test/" for hit in hits)

    def test_search_falls_back_when_query_points_is_absent(self, qdrant):
        """qdrant-client 1.8/1.9 expose only ``search``."""

        class _OldClient:
            def __init__(self):
                self.used = False

            async def search(self, **kwargs):
                self.used = True
                return []

            async def close(self):
                return None

        old = _OldClient()
        qdrant._client = old
        run_async(qdrant.search(_vector(), limit=1))
        assert old.used is True

    def test_delete_page_removes_only_that_document(self, qdrant):
        run_async(qdrant.ensure_collection())
        run_async(qdrant.upsert_embeddings("https://a.test/", ["x"], [_vector()]))
        run_async(qdrant.upsert_embeddings("https://b.test/", ["y"], [_vector()]))
        run_async(qdrant.delete_page("https://a.test/"))
        assert run_async(qdrant.count_points("https://a.test/")) == 0
        assert run_async(qdrant.count_points("https://b.test/")) == 1

    def test_length_mismatch_is_rejected(self, qdrant):
        run_async(qdrant.ensure_collection())
        with pytest.raises(ValueError, match="length mismatch"):
            run_async(qdrant.upsert_embeddings("https://a.test/", ["a", "b"], [_vector()]))

    def test_wrong_vector_dim_is_rejected_before_writing(self, qdrant):
        run_async(qdrant.ensure_collection())
        with pytest.raises(ValueError, match="384"):
            run_async(qdrant.upsert_embeddings("https://a.test/", ["a"], [[0.0] * 128]))
        assert run_async(qdrant.count_points()) == 0

    def test_wrong_query_dim_is_rejected(self, qdrant):
        run_async(qdrant.ensure_collection())
        with pytest.raises(ValueError, match="384"):
            run_async(qdrant.search([0.0] * 10))

    def test_empty_input_is_a_noop(self, qdrant):
        run_async(qdrant.ensure_collection())
        assert run_async(qdrant.upsert_embeddings("https://a.test/", [], [])) == 0


# ==========================================================================
# PostgreSQL contract (instrumented double)
# ==========================================================================
class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    async def set_type_codec(self, *args, **kwargs):
        self.sink["codec"] = (args, kwargs)

    async def execute(self, query):
        self.sink["ddl"].append(query)
        return "OK"

    async def fetchval(self, query):
        self.sink["fetchval"].append(query)
        return 1 if query.strip() == "SELECT 1" else 7


class _FakeAcquire:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return _FakeConn(self.sink)

    async def __aexit__(self, *args):
        return False


class _FakePool:
    """asyncpg.Pool proxies these query methods; the double must too."""

    def __init__(self, sink):
        self.sink = sink

    def acquire(self):
        return _FakeAcquire(self.sink)

    async def fetchrow(self, query, *args):
        self.sink["fetchrow"].append((query, args))
        return {"id": 42, "url": "u"}

    async def fetch(self, query, *args):
        self.sink["fetch"].append((query, args))
        return [{"id": 1, "url": "u"}]

    async def executemany(self, query, rows):
        self.sink["executemany"].append((query, rows))

    async def fetchval(self, query):
        self.sink["fetchval"].append(query)
        return 1 if query.strip() == "SELECT 1" else 7

    async def close(self):
        self.sink["closed"] = True


@pytest.fixture
def postgres():
    sink = {"ddl": [], "fetchrow": [], "fetch": [], "executemany": [], "fetchval": []}
    adapter = PostgresAdapter(dsn="postgresql://u:p@h/db")
    adapter._pool = _FakePool(sink)
    adapter._connected = True
    return adapter, sink


class TestPostgresAdapter:
    def test_upsert_returns_row_id(self, postgres):
        adapter, _ = postgres
        assert run_async(adapter.upsert_crawled_page("https://a.test/", markdown="# Hi")) == 42

    def test_upsert_uses_conflict_target(self, postgres):
        adapter, sink = postgres
        run_async(adapter.upsert_crawled_page("https://a.test/"))
        query, _args = sink["fetchrow"][0]
        assert "ON CONFLICT (url_hash) DO UPDATE" in query

    def test_upsert_uses_asyncpg_placeholders(self, postgres):
        adapter, sink = postgres
        run_async(adapter.upsert_crawled_page("https://a.test/"))
        query, _args = sink["fetchrow"][0]
        assert "$1" in query and "%s" not in query

    def test_url_is_bound_not_inlined(self, postgres):
        adapter, sink = postgres
        run_async(adapter.upsert_crawled_page("https://a.test/"))
        query, args = sink["fetchrow"][0]
        assert "https://a.test/" not in query
        assert url_fingerprint("https://a.test/") in args

    def test_insert_entities_skips_invalid_rows(self, postgres):
        adapter, sink = postgres
        written = run_async(adapter.insert_entities(1, [
            {"name": "OpenAI", "entity_type": "org", "properties": {"k": 1}},
            {"name": "   "},
            {"no_name": 1},
            "not a dict",
        ]))
        assert written == 1
        _query, rows = sink["executemany"][0]
        assert rows[0][2] == "ORG"

    def test_insert_entities_noop_on_empty(self, postgres):
        adapter, sink = postgres
        assert run_async(adapter.insert_entities(1, [])) == 0
        assert not sink["executemany"]

    def test_reads_return_dicts(self, postgres):
        adapter, _ = postgres
        assert run_async(adapter.get_crawled_page("https://a.test/"))["url"] == "u"
        assert run_async(adapter.list_crawled_pages("j1", 10, 0))[0]["id"] == 1
        assert run_async(adapter.list_entities(1))[0]["id"] == 1

    def test_count_rows(self, postgres):
        adapter, _ = postgres
        assert run_async(adapter.count_rows("crawled_pages")) == 7

    def test_count_rows_is_whitelisted(self, postgres):
        adapter, _ = postgres
        with pytest.raises(ValueError, match="Unsupported table"):
            run_async(adapter.count_rows("pg_catalog.pg_authid"))

    def test_ping_is_true(self, postgres):
        adapter, _ = postgres
        assert run_async(adapter.ping()) is True

    def test_schema_ddl_is_idempotent(self):
        from byconn.storage.adapters import SCHEMA_SQL

        assert "CREATE TABLE IF NOT EXISTS crawled_pages" in SCHEMA_SQL
        assert "CREATE TABLE IF NOT EXISTS extracted_entities" in SCHEMA_SQL
        assert SCHEMA_SQL.count("IF NOT EXISTS") >= 6
        assert "ON DELETE CASCADE" in SCHEMA_SQL

    def test_jsonb_codec_registered_on_connect(self, monkeypatch):
        sink = {"ddl": []}
        captured = {}

        async def fake_create_pool(**kwargs):
            captured.update(kwargs)
            await kwargs["init"](_FakeConn(sink))   # asyncpg awaits init(conn)
            return _FakePool(sink)

        import asyncpg

        monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
        adapter = PostgresAdapter(dsn="postgresql://u:p@h/db")
        run_async(adapter.connect())
        assert {"dsn", "min_size", "max_size", "command_timeout", "init"} <= set(captured)
        assert sink["codec"][1]["schema"] == "pg_catalog"
        assert sink["codec"][1]["decoder"].__name__ == "loads"
        assert sink["ddl"], "schema DDL must run on connect"


# ==========================================================================
# Neo4j contract (instrumented double)
# ==========================================================================
class _FakeResult:
    def __init__(self, rows):
        self.rows = rows

    async def single(self):
        return self.rows[0] if self.rows else None

    async def data(self):
        return self.rows


class _FakeTx:
    def __init__(self, sink):
        self.sink = sink

    async def run(self, query, **params):
        self.sink["cypher"].append((query, params))
        return _FakeResult([])


class _FakeSession:
    def __init__(self, sink, database=None):
        self.sink = sink
        self.database = database

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def run(self, query, **params):
        self.sink["cypher"].append((query, params))
        return _FakeResult(self.sink.get("rows", []))

    async def execute_write(self, fn, *args, **kwargs):
        return await fn(_FakeTx(self.sink))


class _FakeDriver:
    def __init__(self, sink):
        self.sink = sink
        self.closed = False

    def session(self, database=None):
        self.sink["database"] = database
        return _FakeSession(self.sink, database)

    async def close(self):
        self.closed = True


@pytest.fixture
def neo4j():
    sink = {"cypher": []}
    adapter = Neo4jAdapter()
    adapter._driver = _FakeDriver(sink)
    adapter._connected = True
    return adapter, sink


class TestNeo4jAdapter:
    def test_write_triple_returns_count(self, neo4j):
        adapter, _ = neo4j
        assert run_async(adapter.write_triple("OpenAI", "employs", "Alice")) == 1

    def test_cypher_merges_nodes_and_relationship(self, neo4j):
        adapter, sink = neo4j
        run_async(adapter.write_triple("OpenAI", "employs", "Alice"))
        query, _params = sink["cypher"][-1]
        assert query.count("MERGE") == 3
        assert "[r:EMPLOYS]" in query

    def test_values_are_parameters_never_inlined(self, neo4j):
        adapter, sink = neo4j
        run_async(adapter.write_triple("OpenAI", "employs", "Alice"))
        query, params = sink["cypher"][-1]
        assert "OpenAI" not in query and "Alice" not in query
        assert params["subject"] == "OpenAI" and params["object"] == "Alice"

    def test_predicate_is_normalised(self, neo4j):
        adapter, sink = neo4j
        run_async(adapter.write_triple("A", "developed by", "B"))
        assert "[r:DEVELOPED_BY]" in sink["cypher"][-1][0]

    def test_custom_labels(self, neo4j):
        adapter, sink = neo4j
        run_async(adapter.write_triples([
            {"subject": "A", "predicate": "HAS", "object": "B", "object_type": "Product"},
        ]))
        assert any("(o:Product" in q for q, _ in sink["cypher"])

    def test_batch_skips_incomplete_triples(self, neo4j):
        adapter, sink = neo4j
        written = run_async(adapter.write_triples([
            {"subject": "A", "predicate": "HAS", "object": "B"},
            {"subject": "", "predicate": "X", "object": "B"},
            {"subject": "A", "predicate": "", "object": "B"},
            "junk",
        ]))
        assert written == 1

    def test_batch_survives_one_poisoned_entry(self, neo4j):
        """A bad predicate must not discard the valid triples beside it."""
        adapter, sink = neo4j
        written = run_async(adapter.write_triples([
            {"subject": "Keep", "predicate": "OK", "object": "Good"},
            {"subject": "Bad", "predicate": "X; DROP DATABASE neo4j", "object": "Evil"},
            {"subject": "Keep2", "predicate": "ALSO_OK", "object": "Good2"},
        ]))
        assert written == 2
        assert not any("DROP" in q for q, _ in sink["cypher"])
        assert any(p.get("subject") == "Keep2" for _q, p in sink["cypher"])

    def test_strict_batch_raises(self, neo4j):
        adapter, _ = neo4j
        with pytest.raises(ValueError):
            run_async(adapter.write_triples(
                [{"subject": "a", "predicate": "X; DROP", "object": "b"}], strict=True
            ))

    def test_strict_single_write_raises(self, neo4j):
        adapter, _ = neo4j
        with pytest.raises(ValueError):
            run_async(adapter.write_triple("a", "BAD;TYPE", "b"))

    def test_link_page_entities(self, neo4j):
        adapter, sink = neo4j
        assert run_async(adapter.link_page_entities("https://a.test/", [
            {"name": "OpenAI", "entity_type": "org"}, {"name": ""}, "junk",
        ])) == 1
        assert any(":MENTIONS" in q for q, _ in sink["cypher"])
        assert any(p.get("type") == "ORG" for _q, p in sink["cypher"])

    def test_neighbours_and_stats(self, neo4j):
        adapter, sink = neo4j
        sink["rows"] = [{"key": "n1", "labels": ["Entity"], "rel": "HAS", "props": {}}]
        assert run_async(adapter.neighbors("OpenAI"))[0]["key"] == "n1"
        sink["rows"] = [{"nodes": 3, "rels": 7}]
        assert run_async(adapter.stats()) == {"nodes": 3, "relationships": 7}

    def test_constraints_created_on_connect(self, monkeypatch):
        sink = {"cypher": []}
        captured = {}

        import neo4j

        def fake_driver(uri, auth=None, **kwargs):
            captured.update({"uri": uri, "auth": auth, **kwargs})
            return _FakeDriver(sink)

        monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", staticmethod(fake_driver))
        adapter = Neo4jAdapter()
        run_async(adapter.connect())
        cypher = " ".join(q for q, _ in sink["cypher"])
        assert "FOR (p:Page) REQUIRE p.url IS UNIQUE" in cypher
        assert "FOR (e:Entity) REQUIRE e.key IS UNIQUE" in cypher
        assert captured["uri"] == DEFAULT_NEO4J_URI
        assert captured["auth"] == ("neo4j", DEFAULT_NEO4J_PASSWORD)
        run_async(adapter.close())

    def test_database_is_routed_when_configured(self, neo4j):
        adapter, sink = neo4j
        adapter.database = "neo4j"
        run_async(adapter.upsert_page("https://a.test/"))
        assert sink["database"] == "neo4j"
