"""Tests for the FastAPI service in server.py.

Every database, the browser and the LLM are replaced by doubles from conftest,
so the suite exercises real routing, validation, the background pipeline and
the health contract without any external service.
"""

import asyncio
import json

import pytest

from byconn.tests.doubles import poll_job, start_crawl

import server as server_module


# ==========================================================================
# pure helpers
# ==========================================================================
class TestExtractLinks:
    HTML = (
        '<a href="/a">A</a><a href="/b">B</a>'
        '<a href="https://other.com/x">External</a>'
        '<a href="#frag">Anchor</a><a href="mailto:x@y.z">Mail</a>'
        '<a href="javascript:void(0)">JS</a><a href="tel:123">Tel</a>'
        '<a href="/a">A again</a>'
    )

    def test_keeps_same_host_absolute_links(self):
        links = server_module.extract_links(self.HTML, "https://example.com/", "example.com")
        assert "https://example.com/a" in links
        assert "https://example.com/b" in links

    def test_drops_offsite_links(self):
        links = server_module.extract_links(self.HTML, "https://example.com/", "example.com")
        assert not any("other.com" in link for link in links)

    def test_drops_non_navigational_schemes(self):
        links = server_module.extract_links(self.HTML, "https://example.com/", "example.com")
        assert not any(
            link.startswith(("mailto:", "javascript:", "tel:", "#"))
            for link in links
        )

    def test_deduplicates(self):
        links = server_module.extract_links(self.HTML, "https://example.com/", "example.com")
        assert len(links) == len(set(links))

    def test_resolves_relative_links(self):
        html = '<a href="deep/page">Deep</a>'
        links = server_module.extract_links(html, "https://example.com/base/", "example.com")
        assert links == ["https://example.com/base/deep/page"]

    def test_handles_malformed_html(self):
        assert server_module.extract_links("<a href=", "https://x.test/", "x.test") == []


class TestGuardedStep:
    def _job(self):
        return server_module.CrawlJob(job_id="j", url="https://a.test/", max_depth=0)

    def test_returns_value_on_success(self):
        job = self._job()

        async def ok():
            return 7

        assert asyncio.run(server_module.guarded_step(job, "u", "step", ok())) == 7
        assert job.errors == []

    def test_records_and_swallows_exception(self):
        job = self._job()

        async def boom():
            raise RuntimeError("db down")

        assert asyncio.run(server_module.guarded_step(job, "u", "postgres save", boom())) is None
        assert "postgres save failed" in job.errors[0]
        assert "db down" in job.errors[0]

    def test_records_timeout(self, monkeypatch):
        monkeypatch.setattr(server_module, "PIPELINE_STEP_TIMEOUT", 0.05)
        job = self._job()

        async def hang():
            await asyncio.sleep(5)

        assert asyncio.run(server_module.guarded_step(job, "u", "qdrant indexing", hang())) is None
        assert "qdrant indexing timed out" in job.errors[0]

    def test_propagates_cancellation(self, monkeypatch):
        monkeypatch.setattr(server_module, "PIPELINE_STEP_TIMEOUT", 5)
        job = self._job()

        async def scenario():
            async def cancel():
                await asyncio.sleep(5)
            task = asyncio.create_task(
                server_module.guarded_step(job, "u", "step", cancel())
            )
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())


class TestCrawlJob:
    def test_to_dict_shape(self):
        job = server_module.CrawlJob(job_id="j1", url="https://a.test/", max_depth=2)
        payload = job.to_dict()
        assert payload["job_id"] == "j1"
        assert payload["status"] == "queued"
        assert payload["errors"] == []
        assert payload["duration_seconds"] is None

    def test_duration_is_computed_once_finished(self):
        job = server_module.CrawlJob(job_id="j1", url="u", max_depth=0)
        job.started_at = 100.0
        job.finished_at = 102.5
        assert job.to_dict()["duration_seconds"] == 2.5


class TestRequestModelValidation:
    @pytest.mark.parametrize("url", [
        "ftp://example.com", "not-a-url", "https://", "", "   ",
    ])
    def test_rejects_non_http_urls(self, url):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            server_module.CrawlBody(url=url, max_depth=1)

    def test_accepts_valid_url(self):
        body = server_module.CrawlBody(url="https://example.com", max_depth=2)
        assert body.url == "https://example.com"
        assert body.max_depth == 2

    def test_default_depth_is_one(self):
        assert server_module.CrawlBody(url="https://example.com").max_depth == 1

    @pytest.mark.parametrize("depth", [-1, 6, 99])
    def test_rejects_out_of_range_depth(self, depth):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            server_module.CrawlBody(url="https://example.com", max_depth=depth)


# ==========================================================================
# HTTP surface
# ==========================================================================
class TestHealthEndpoint:
    def test_healthy_service_returns_200(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert {c["name"] for c in body["components"]} == {"postgres", "qdrant", "neo4j"}

    def test_reports_llm_and_embedding_providers(self, client):
        body = client.get("/api/v1/health").json()
        assert body["llm"]["status"] in {"up", "disabled"}
        assert body["embeddings"]["status"] in {"up", "disabled"}

    def test_unavailable_embedding_provider_is_reported(self, client):
        body = client.get("/api/v1/health").json()
        if body["embeddings"]["status"] == "disabled":
            assert "embedding provider" in body["embeddings"]["detail"]

    def test_degraded_when_a_dependency_is_down(self, client):
        client.app.state.neo4j.connected = False
        response = client.get("/api/v1/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert any(c["name"] == "neo4j" and c["status"] == "down" for c in body["components"])

    def test_recovers_to_200(self, client):
        client.app.state.neo4j.connected = False
        client.get("/api/v1/health")
        client.app.state.neo4j.connected = True
        assert client.get("/api/v1/health").status_code == 200

    def test_reports_active_jobs(self, client):
        assert client.get("/api/v1/health").json()["active_jobs"] == 0


class TestCrawlEndpointValidation:
    @pytest.mark.parametrize("payload", [
        {"url": "ftp://example.com", "max_depth": 1},
        {"url": "not-a-url", "max_depth": 1},
        {"url": "https://example.com", "max_depth": -1},
        {"url": "https://example.com", "max_depth": 99},
        {},
    ])
    def test_rejects_bad_requests_with_422(self, client, payload):
        assert client.post("/api/v1/crawl", json=payload).status_code == 422

    def test_accepts_valid_request(self, client):
        response = client.post("/api/v1/crawl",
                               json={"url": "https://example.com/", "max_depth": 0})
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert len(body["job_id"]) == 32
        poll_job(client, body["job_id"])

    def test_max_depth_defaults(self, client):
        response = client.post("/api/v1/crawl", json={"url": "https://example.com/"})
        assert response.status_code == 202
        assert response.json()["max_depth"] == 1
        poll_job(client, response.json()["job_id"])

    def test_unknown_job_returns_404(self, client):
        assert client.get("/api/v1/crawl/does-not-exist").status_code == 404


class TestCrawlPipeline:
    def _state(self, client):
        return (client.app.state.postgres, client.app.state.qdrant,
                client.app.state.neo4j, client.app.state.extractor)

    def test_depth_zero_crawls_a_single_page(self, client):
        postgres, qdrant, neo4j, extractor = self._state(client)
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert job["status"] == "succeeded"
        assert job["pages_crawled"] == 1
        assert len(postgres.pages) == 1

    def test_raw_html_is_persisted(self, client):
        postgres, _qdrant, _neo4j, _extractor = self._state(client)
        poll_job(client, start_crawl(client, "https://example.com/", 0))
        stored = postgres.pages[0]
        # The raw document, not the cleaned copy.
        assert "<script>" in stored["markdown"]
        assert stored["title"] == "Home"
        assert stored["depth"] == 0

    def test_cleaned_html_feeds_the_llm(self, client):
        _postgres, _qdrant, _neo4j, extractor = self._state(client)
        poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert len(extractor.seen_text) == 1
        assert "<script>" not in extractor.seen_text[0]
        assert "<style>" not in extractor.seen_text[0]
        assert "Welcome to Example Corp." in extractor.seen_text[0]

    def test_entities_and_relations_are_stored(self, client):
        postgres, _qdrant, neo4j, _extractor = self._state(client)
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert job["entities_extracted"] == 2
        assert job["relations_written"] == 1
        assert len(postgres.entities) == 1
        assert len(postgres.entities[0][1]) == 2
        assert neo4j.pages == ["https://example.com/"]
        assert len(neo4j.triples) == 1
        assert neo4j.triples[0][1]["source_url"] == "https://example.com/"

    def test_chunks_are_indexed_in_qdrant(self, client):
        _postgres, qdrant, _neo4j, _extractor = self._state(client)
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert job["chunks_indexed"] >= 1
        assert qdrant.upserts[0]["url"] == "https://example.com/"

    def test_depth_one_follows_same_host_links_only(self, client):
        postgres, _qdrant, _neo4j, _extractor = self._state(client)
        job = poll_job(client, start_crawl(client, "https://example.com/", 1))
        urls = sorted(page["url"] for page in postgres.pages)
        assert urls == ["https://example.com/", "https://example.com/a", "https://example.com/b"]
        assert not any("other.com" in url for url in urls)
        assert job["pages_crawled"] == 3

    def test_depth_two_reaches_grandchild(self, client):
        postgres, _qdrant, _neo4j, _extractor = self._state(client)
        job = poll_job(client, start_crawl(client, "https://example.com/", 2))
        urls = sorted(page["url"] for page in postgres.pages)
        assert "https://example.com/c" in urls
        assert job["pages_crawled"] == 4
        assert any(page["depth"] == 2 for page in postgres.pages)

    def test_page_cap_is_a_hard_limit(self, client, monkeypatch):
        monkeypatch.setattr(server_module, "CRAWL_MAX_PAGES", 2)
        postgres, _qdrant, _neo4j, _extractor = self._state(client)
        job = poll_job(client, start_crawl(client, "https://example.com/", 3))
        assert job["pages_crawled"] <= 2
        assert len(postgres.pages) <= 2

    def test_robots_disallow_is_honoured(self, client, monkeypatch):
        from urllib.robotparser import RobotFileParser

        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /a"])

        async def _robots(_base_url):
            return parser

        monkeypatch.setattr(server_module, "load_robots", _robots)
        postgres, _qdrant, _neo4j, _extractor = self._state(client)
        poll_job(client, start_crawl(client, "https://example.com/", 2))
        urls = [page["url"] for page in postgres.pages]
        assert "https://example.com/a" not in urls
        assert "https://example.com/b" in urls


class TestHonestCounters:
    """A counter must never report work that was not actually persisted."""

    def test_relations_written_is_zero_when_neo4j_rejects(self, client, monkeypatch):
        """Regression: extraction is not persistence.

        The pipeline used to add the number of *extracted* triples, so a crawl
        whose every Neo4j write failed still reported relations_written > 0.
        """
        _pg, _qd, neo4j, _ex = (
            client.app.state.postgres,
            client.app.state.qdrant,
            client.app.state.neo4j,
            client.app.state.extractor,
        )

        async def _refuse(*_args, **_kwargs):
            raise RuntimeError("neo4j unavailable")

        monkeypatch.setattr(neo4j, "write_triples", _refuse)
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))

        assert job["status"] == "succeeded", "a dead store must not fail the crawl"
        assert job["entities_extracted"] == 2, "extraction still happened"
        assert job["relations_written"] == 0, "nothing was persisted, so nothing to report"
        assert any("neo4j relations failed" in err for err in job["errors"])

    def test_relations_written_counts_only_accepted_triples(self, client, monkeypatch):
        _pg, _qd, neo4j, _ex = (
            client.app.state.postgres,
            client.app.state.qdrant,
            client.app.state.neo4j,
            client.app.state.extractor,
        )

        async def _partial(_triples, **_kwargs):
            return 0  # store accepted nothing

        monkeypatch.setattr(neo4j, "write_triples", _partial)
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert job["relations_written"] == 0


class TestHealthAlias:
    """Standard liveness probes expect /health, not only the versioned path."""

    def test_both_paths_are_registered(self):
        from server import app

        paths = {route.path for route in app.routes}
        assert "/api/v1/health" in paths
        assert "/health" in paths

    def test_alias_answers_like_the_versioned_path(self, client):
        aliased = client.get("/health")
        versioned = client.get("/api/v1/health")
        assert aliased.status_code == versioned.status_code
        assert aliased.json()["status"] == versioned.json()["status"]


class TestCrawlResilience:
    def test_database_failure_does_not_abort_the_crawl(self, client):
        postgres, qdrant, _neo4j, _extractor = (
            client.app.state.postgres, client.app.state.qdrant,
            client.app.state.neo4j, client.app.state.extractor,
        )
        postgres.fail = True
        qdrant.fail = True
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert job["status"] == "succeeded"
        assert job["pages_crawled"] == 1
        assert job["pages_saved"] == 0
        joined = " | ".join(job["errors"])
        assert "postgres save failed" in joined
        assert "qdrant indexing failed" in joined

    def test_graph_is_written_even_when_sql_is_down(self, client):
        postgres, _qdrant, neo4j, _extractor = (
            client.app.state.postgres, client.app.state.qdrant,
            client.app.state.neo4j, client.app.state.extractor,
        )
        postgres.fail = True
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert job["relations_written"] == 1
        assert len(neo4j.triples) == 1
        assert "entity insert failed" in " | ".join(job["errors"])

    def test_unavailable_extractor_skips_extraction(self, client):
        extractor = client.app.state.extractor
        extractor.available = False
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert job["status"] == "succeeded"
        assert job["entities_extracted"] == 0
        assert extractor.seen_text == []

    def test_crawls_run_concurrently(self, client):
        postgres = client.app.state.postgres
        first = start_crawl(client, "https://example.com/", 0)
        second = start_crawl(client, "https://example.com/b", 0)
        assert first != second
        assert poll_job(client, first)["status"] == "succeeded"
        assert poll_job(client, second)["status"] == "succeeded"
        assert len(postgres.pages) == 2


class TestDashboardAndDocs:
    def test_dashboard_index_is_served(self, client):
        response = client.get("/dashboard/index.html")
        assert response.status_code == 200
        assert "BYCONN-X" in response.text

    def test_dashboard_assets_are_served(self, client):
        assert client.get("/dashboard/app.js").status_code == 200
        assert client.get("/dashboard/style.css").status_code == 200

    def test_root_redirects_to_dashboard(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert response.headers["location"].endswith("/dashboard/index.html")

    def test_app_js_is_synced_with_the_html(self):
        """Guards against the historical app.js/index.html contract break."""
        import pathlib
        import re

        root = pathlib.Path(server_module.__file__).resolve().parent
        html = (root / "byconn" / "dashboard" / "index.html").read_text()
        js = (root / "byconn" / "dashboard" / "app.js").read_text()

        nav_tabs = set(re.findall(r'data-tab="([^"]+)"', html))
        panels = set(re.findall(r'id="panel-([^"]+)"', html))
        assert nav_tabs == panels, "every nav entry must have a matching panel"

        meta = set(re.findall(r'^\s{4}"?([a-z-]+)"?: \{', js[js.index("TAB_META"):], re.M))
        assert nav_tabs <= meta, f"TAB_META missing {nav_tabs - meta}"

        referenced = set(re.findall(r'getElementById\("([^"]+)"\)', js))
        assert referenced <= set(re.findall(r'id="([^"]+)"', html)), \
            f"app.js targets missing ids: {referenced - set(re.findall(chr(34) + 'id=([^ ]+)', html))}"

        # Only one tab controller: no competing inline script.
        assert "<script>" not in html, "tab logic must live in app.js only"

    # --- dashboard defects found by running the page against the server -----

    @staticmethod
    def _dashboard():
        """Reads index.html and app.js with HTML comments stripped."""
        import pathlib
        import re

        root = pathlib.Path(server_module.__file__).resolve().parent
        html = (root / "byconn" / "dashboard" / "index.html").read_text()
        live = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        return live, (root / "byconn" / "dashboard" / "app.js").read_text()

    def test_engine_indicator_does_not_claim_online_before_the_probe(self):
        """It used to ship a hardcoded ONLINE that lied for the probe duration."""
        live, js = self._dashboard()
        assert "ONLINE" not in live.upper().split("STATUS-INDICATOR")[1][:200], (
            "the indicator must not pre-render a success state"
        )
        assert "CHECKING" in live
        # And app.js must set the pending state before awaiting the request.
        pending = js.index('setEngineStatus("CHECKING...")')
        awaited = js.index("await fetch(API_BASE + \"/health\")")
        assert pending < awaited, "the pending state must be set before the await"

    def test_every_live_button_has_a_handler(self):
        """Eight decorative buttons shipped in the original layout."""
        import re

        live, js = self._dashboard()
        labels = [
            re.sub(r"\s+", " ", m).strip()
            for m in re.findall(r"<button[^>]*>(.*?)</button>", live, re.S)
        ]
        assert labels, "expected the dashboard to keep its working buttons"
        # Each live button is reachable either by an inline handler that
        # setupSearch rewires, or by an addEventListener in app.js.
        wired = ("startSearch", "clearSearch", "setupHealthIndicator",
                 "loadDiscoveredApis")
        for handler in wired:
            assert handler in js, f"{handler} is missing from app.js"
        for label in labels:
            assert not re.search(r"\b(Export|Delete|Save|History)\b", label), (
                f"unimplemented control is still visible: {label!r}"
            )

    def test_visual_action_mode_calls_the_agent_endpoint(self):
        """It used to only lower max_depth, so /act was unreachable from the UI."""
        _, js = self._dashboard()
        assert '"Visual Action": { kind: "agent"' in js
        assert "isAgent" in js and 'isAgent ? "act" : "crawl"' in js
        # The agent payload must carry the task the endpoint requires.
        assert "task: target.task" in js

    def test_bare_domains_are_accepted(self):
        """Typing example.com used to do nothing at all."""
        _, js = self._dashboard()
        assert "function parseTarget" in js
        for token in ("localhost", "example.com", "https://"):
            assert token in js
        # The old scheme-only matcher must be gone.
        assert "function extractUrl" not in js

    def test_api_discovery_tab_is_wired_to_the_registry(self):
        live, js = self._dashboard()
        assert 'data-tab="api-discovery"' in live
        assert 'id="panel-api-discovery"' in live
        assert "${API_BASE}/apis" in js

    def test_openapi_documents_the_contract(self, client):
        spec = client.get("/openapi.json").json()
        assert "/api/v1/crawl" in spec["paths"]
        assert "/api/v1/health" in spec["paths"]
        assert "/api/v1/crawl/{job_id}" in spec["paths"]
        body_ref = spec["paths"]["/api/v1/crawl"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"].split("/")[-1]
        properties = spec["components"]["schemas"][body_ref]["properties"]
        assert "url" in properties
        assert properties["max_depth"]["type"] == "integer"
        assert properties["max_depth"]["default"] == 1


class TestShutdown:
    def test_adapters_are_closed_on_lifespan_exit(self, server_app):
        app, TestClient = server_app
        with TestClient(app) as test_client:
            assert test_client.get("/api/v1/health").status_code == 200
        assert app.state.postgres.connected is False
        assert app.state.qdrant.connected is False
        assert app.state.neo4j.connected is False


class TestPipelineConcurrency:
    """Independent per-page steps overlap instead of running back to back."""

    def test_page_save_and_llm_extraction_overlap(self, client, monkeypatch):
        state = client.app.state
        extraction_started = asyncio.Event()
        original_save = state.postgres.upsert_crawled_page
        original_extract = state.extractor.extract_knowledge

        async def save(**kwargs):
            # Sequential steps would deadlock here: extraction only starts
            # after the save returns, so the wait times out and nothing saves.
            await asyncio.wait_for(extraction_started.wait(), timeout=2)
            return await original_save(**kwargs)

        async def extract(text):
            extraction_started.set()
            return await original_extract(text)

        monkeypatch.setattr(state.postgres, "upsert_crawled_page", save)
        monkeypatch.setattr(state.extractor, "extract_knowledge", extract)
        job = poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert job["pages_saved"] == 1
        assert job["entities_extracted"] == 2

    def test_real_status_code_is_stored(self, client):
        poll_job(client, start_crawl(client, "https://example.com/", 0))
        # FakeCrawler performs no navigation, so no status is known: it must be
        # stored as unknown rather than invented as 200.
        assert client.app.state.postgres.pages[0].get("status_code") is None


class TestSeedRobots:
    def test_disallowed_seed_is_not_crawled(self, client, monkeypatch):
        from urllib.robotparser import RobotFileParser

        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /"])

        async def _robots(_base_url):
            return parser

        monkeypatch.setattr(server_module, "load_robots", _robots)
        job = poll_job(client, start_crawl(client, "https://example.com/", 1))
        assert job["status"] == "failed"
        assert job["pages_crawled"] == 0
        assert any("robots.txt disallows" in e for e in job["errors"])


SCHEMA = {
    "type": "object",
    "properties": {"pages": {"type": "array", "items": {"type": "object"}}},
}


def _start(client, **body):
    response = client.post("/api/v1/crawl", json={"url": "https://example.com/", **body})
    assert response.status_code == 202, response.text
    return response.json()


class TestStructuredCrawl:
    def test_prompt_and_schema_reach_the_extractor_and_merge(self, client):
        accepted = _start(client, max_depth=1, prompt="list the pages", schema=SCHEMA)
        assert accepted["prompt"] == "list the pages" and accepted["has_schema"] is True
        job = poll_job(client, accepted["job_id"])
        calls = client.app.state.extractor.structured_calls
        assert calls and calls[0]["instruction"] == "list the pages"
        assert calls[0]["schema"] == SCHEMA
        stored = client.app.state.jobs[accepted["job_id"]]
        urls = sorted(p["url"] for p in stored.structured["data"]["pages"])
        assert urls == ["https://example.com/", "https://example.com/a", "https://example.com/b"]
        assert job["fields_verified"] == 3 and job["fields_unverified"] == 0

    def test_plain_crawl_does_not_run_structured_extraction(self, client):
        poll_job(client, start_crawl(client, "https://example.com/", 0))
        assert client.app.state.extractor.structured_calls == []

    @pytest.mark.parametrize("schema", [{"type": "array"}, {"type": "object", "properties": 3}])
    def test_invalid_schema_is_rejected(self, client, schema):
        response = client.post("/api/v1/crawl", json={"url": "https://example.com/", "schema": schema})
        assert response.status_code == 422

    def test_page_results_are_kept_and_persisted(self, client):
        job_id = _start(client, max_depth=0, prompt="x")["job_id"]
        poll_job(client, job_id)
        page = client.app.state.jobs[job_id].pages[0]
        assert page["summary"] == "An example page."
        assert page["entities"][0]["name"] == "Example Corp"
        stored = client.app.state.postgres.extraction_results
        assert stored[0]["job_id"] == job_id and stored[0]["structured"]["data"]

    def test_results_survive_a_dead_postgres(self, client):
        client.app.state.postgres.fail = True
        job_id = _start(client, max_depth=0, prompt="x")["job_id"]
        job = poll_job(client, job_id)
        assert job["status"] == "succeeded"
        assert client.app.state.jobs[job_id].pages[0]["structured"]["data"]
        assert any("result insert failed" in e for e in job["errors"])


def _read_events(client, path):
    events = []
    with client.stream("GET", path) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        name = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[len("data: "):])))
    return events


class TestEventStream:
    def test_live_crawl_streams_snapshot_pages_then_done(self, client):
        client.app.state.extractor.delay = 0.3
        job_id = _start(client, max_depth=0)["job_id"]
        events = _read_events(client, f"/api/v1/crawl/{job_id}/events")
        names = [name for name, _ in events]
        assert names[0] == "snapshot" and names[-1] == "done"
        assert "page" in names
        page = next(data for name, data in events if name == "page")
        assert page["page"]["url"] == "https://example.com/"
        assert events[-1][1]["status"] == "succeeded"

    def test_finished_job_gets_snapshot_and_done_immediately(self, client):
        job_id = start_crawl(client, "https://example.com/", 0)
        poll_job(client, job_id)
        names = [name for name, _ in _read_events(client, f"/api/v1/crawl/{job_id}/events")]
        assert names == ["snapshot", "done"]

    def test_errors_are_streamed_once(self, client):
        client.app.state.postgres.fail = True
        client.app.state.extractor.delay = 0.3
        job_id = _start(client, max_depth=0)["job_id"]
        events = _read_events(client, f"/api/v1/crawl/{job_id}/events")
        messages = [data["message"] for name, data in events if name == "error"]
        assert messages and len(messages) == len(set(messages))

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/v1/crawl/nope/events").status_code == 404
        assert client.get("/api/v1/act/nope/events").status_code == 404

    def test_subscribers_are_released(self, client):
        job_id = start_crawl(client, "https://example.com/", 0)
        poll_job(client, job_id)
        _read_events(client, f"/api/v1/crawl/{job_id}/events")
        assert client.app.state.jobs[job_id].events.subscriber_count == 0


class TestResultsApi:
    def test_results_carry_pages_structured_and_a_real_graph(self, client):
        job_id = _start(client, max_depth=1, prompt="x", schema=SCHEMA)["job_id"]
        poll_job(client, job_id)
        body = client.get(f"/api/v1/crawl/{job_id}/results").json()
        assert body["job"]["job_id"] == job_id
        assert len(body["pages"]) == 3
        assert len(body["structured"]["data"]["pages"]) == 3
        graph = body["graph"]
        # FakeExtractor returns the same two entities for every page: they
        # must be deduplicated, typed, and joined by the one real relation.
        assert sorted(n["label"] for n in graph["nodes"]) == ["Alice", "Example Corp"]
        types = {n["label"]: n["type"] for n in graph["nodes"]}
        assert types == {"Alice": "PERSON", "Example Corp": "ORGANIZATION"}
        assert [e["type"] for e in graph["edges"]] == ["EMPLOYS"]
        assert graph["truncated"] is False

    def test_results_fall_back_to_postgres_after_eviction(self, client):
        job_id = _start(client, max_depth=0, prompt="x")["job_id"]
        poll_job(client, job_id)
        client.app.state.jobs.pop(job_id)
        body = client.get(f"/api/v1/crawl/{job_id}/results").json()
        assert body["job"]["status"] == "archived"
        assert body["pages"][0]["url"] == "https://example.com/"

    def test_unknown_results_are_404(self, client):
        assert client.get("/api/v1/crawl/nope/results").status_code == 404

    def test_graph_is_capped(self):
        pages = [{"url": "u", "entities": [{"name": f"e{i}", "type": "CONCEPT"} for i in range(20)],
                  "triples": [{"subject": "e0", "predicate": "LINKS", "object": "e19"}]}]
        graph = server_module.build_graph(pages, cap=10)
        assert len(graph["nodes"]) == 10 and graph["truncated"] is True
        assert graph["edges"] == []  # e19 did not fit, so no dangling edge

    def test_jobs_lists_newest_first(self, client):
        first = start_crawl(client, "https://example.com/", 0)
        second = start_crawl(client, "https://example.com/", 0)
        poll_job(client, first)
        poll_job(client, second)
        jobs = client.get("/api/v1/jobs").json()["jobs"]
        assert [j["job_id"] for j in jobs[:2]] == [second, first]
        assert all(j["kind"] == "crawl" for j in jobs)


class TestExport:
    def _job(self, client):
        job_id = _start(client, max_depth=1, prompt="x", schema=SCHEMA)["job_id"]
        poll_job(client, job_id)
        return job_id

    def test_json_download(self, client):
        job_id = self._job(client)
        response = client.get(f"/api/v1/crawl/{job_id}/export?format=json")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert json.loads(response.text)["job"]["job_id"] == job_id

    def test_jsonl_has_one_page_per_line(self, client):
        job_id = self._job(client)
        lines = client.get(f"/api/v1/crawl/{job_id}/export?format=jsonl").text.splitlines()
        assert len(lines) == 3 and all(json.loads(line)["url"] for line in lines)

    def test_csv_flattens_the_array_with_sources(self, client):
        job_id = self._job(client)
        response = client.get(f"/api/v1/crawl/{job_id}/export?format=csv")
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
        rows = response.text.strip().splitlines()
        assert rows[0] == "url,sources"
        assert len(rows) == 4

    def test_csv_without_a_table_is_400(self, client):
        job_id = start_crawl(client, "https://example.com/", 0)
        poll_job(client, job_id)
        response = client.get(f"/api/v1/crawl/{job_id}/export?format=csv")
        assert response.status_code == 400 and "array of objects" in response.json()["detail"]

    def test_unknown_format_is_400(self, client):
        job_id = self._job(client)
        assert client.get(f"/api/v1/crawl/{job_id}/export?format=xml").status_code == 400

    def test_csv_neutralises_formulas(self):
        result = {"data": {"rows": [{"a": "=HYPERLINK(\"x\")", "b": 2}]}, "citations": {}}
        _field, columns, rows = server_module.structured_table(result)
        assert columns == ["a", "b", "sources"]
        assert rows[0][0].startswith("'=") and rows[0][1] == "2"


class TestHealthDetail:
    def test_llm_detail_names_the_model_but_never_a_key(self, client):
        body = client.get("/api/v1/health").json()
        assert body["llm"]["detail"] == "openai · fake-model"
        assert "sk-" not in json.dumps(body) and "gsk_" not in json.dumps(body)


def test_rerunning_a_crawl_is_not_deduplicated_against_the_first(client):
    """Dedup state was shared by every job, so a second crawl of the same site
    skipped every page as a duplicate and returned nothing."""
    first = poll_job(client, start_crawl(client, "https://example.com/", 0))
    second = poll_job(client, start_crawl(client, "https://example.com/", 0))
    assert first["duplicates_skipped"] == 0
    assert second["duplicates_skipped"] == 0
    assert second["entities_extracted"] == first["entities_extracted"] > 0
