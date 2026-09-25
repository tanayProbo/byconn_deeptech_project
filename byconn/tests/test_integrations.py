"""Tests for the P2 integrations: api_intelligence, visual_agent and dedup.

Covers the wiring added to server.py, browser_pool.py and main.py, plus the
newly de-ClickHouse'd health monitor and shared link discovery.
"""

import json

import pytest

from byconn.api_intelligence.proxy_sniffer import DiscoveredEndpoint, ProxySniffer
from byconn.api_intelligence.schema_generator import SchemaGenerator
from byconn.core.link_discovery import (
    extract_links,
    is_allowed,
    load_robots,
)
from byconn.core.browser_pool import BrowserPool, attach_api_intelligence
from byconn.pipeline.cleaning import DataCleaner
from byconn.visual_agent.agent_loop import VisualBrowserAgent
from byconn.visual_agent.planner import (
    MAX_IMAGE_BYTES,
    HeuristicPlanner,
    LLMPlanner,
    build_planner,
    normalise_action,
)
from byconn.free_api_integration import APIHealthMonitor, APIRegistry

from byconn.tests.doubles import poll_job, run_async

import server as server_module


# ==========================================================================
# health monitor: ClickHouse removed, Postgres + logging used instead
# ==========================================================================
class TestHealthMonitorNoClickHouse:
    def test_clickhouse_parameter_is_gone(self):
        import inspect

        params = inspect.signature(APIHealthMonitor.__init__).parameters
        assert "clickhouse_client" not in params
        assert "postgres" in params

    def test_defaults_to_logging_only(self):
        monitor = APIHealthMonitor(APIRegistry())
        assert monitor.postgres is None

    def test_uses_postgres_when_provided(self):
        class _PG:
            def __init__(self):
                self.rows = []

            def record_api_health(self, **kwargs):
                self.rows.append(kwargs)

        pg = _PG()
        monitor = APIHealthMonitor(APIRegistry(), postgres=pg)
        monitor._record({
            "api_id": "api-x", "api_name": "X", "url": "https://x.test/",
            "status_code": 200, "latency_ms": 42, "is_up": True, "error_message": "",
        })
        assert len(pg.rows) == 1
        assert pg.rows[0]["api_id"] == "api-x"
        assert pg.rows[0]["is_up"] is True

    def test_logs_when_no_postgres(self, caplog):
        monitor = APIHealthMonitor(APIRegistry())
        with caplog.at_level("INFO", logger="byconnx.free_api.monitor"):
            monitor._record({
                "api_id": "api-y", "api_name": "Y", "url": "https://y.test/",
                "status_code": 500, "latency_ms": 10, "is_up": False, "error_message": "boom",
            })
        assert any("api-y" in r.getMessage() for r in caplog.records)

    def test_down_endpoint_logs_a_warning(self, caplog):
        monitor = APIHealthMonitor(APIRegistry())
        with caplog.at_level("WARNING", logger="byconnx.free_api.monitor"):
            monitor._record({
                "api_id": "api-z", "api_name": "Z", "url": "https://z.test/",
                "status_code": 0, "latency_ms": 5, "is_up": False, "error_message": "refused",
            })
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_persistence_failure_does_not_raise(self):
        class _Broken:
            def record_api_health(self, **kwargs):
                raise RuntimeError("db down")

        monitor = APIHealthMonitor(APIRegistry(), postgres=_Broken())
        monitor._record({
            "api_id": "a", "api_name": "A", "url": "u",
            "status_code": 200, "latency_ms": 1, "is_up": True, "error_message": "",
        })  # must not raise

    def test_adapter_without_the_method_is_tolerated(self):
        monitor = APIHealthMonitor(APIRegistry(), postgres=object())
        monitor._record({
            "api_id": "a", "api_name": "A", "url": "u",
            "status_code": 200, "latency_ms": 1, "is_up": True, "error_message": "",
        })  # must not raise

    def test_missing_api_raises(self):
        monitor = APIHealthMonitor(APIRegistry())
        with pytest.raises(ValueError, match="No API found"):
            run_async(monitor.check_api("does-not-exist"))

    def test_placeholder_paths_are_substituted(self):
        monitor = APIHealthMonitor(APIRegistry())
        api = monitor.registry.get_api("api-chain-14")
        assert "{block_hash}" in api["endpoints"][0]["path"]
        resolved = api["endpoints"][0]["path"]
        for placeholder, value in (
            ("{block_hash}", "0" * 64), ("{page}", "1"), ("{id}", "1"), ("{query}", "test")
        ):
            resolved = resolved.replace(placeholder, value)
        assert "{" not in resolved


# ==========================================================================
# link discovery (shared by CLI and API)
# ==========================================================================
class TestLinkDiscovery:
    HTML = (
        '<a href="/a">A</a><a href="https://other.test/x">Off</a>'
        '<a href="#f">Frag</a><a href="mailto:a@b.c">Mail</a>'
        '<a href="javascript:0">JS</a><a href="/a">Dup</a>'
    )

    def test_same_host_only(self):
        links = extract_links(self.HTML, "https://example.test/", "example.test")
        assert links == ["https://example.test/a"]

    def test_relative_resolution(self):
        links = extract_links('<a href="deep/x">D</a>', "https://example.test/base/", "example.test")
        assert links == ["https://example.test/base/deep/x"]

    def test_host_defaults_to_base_url(self):
        links = extract_links('<a href="/a">A</a>', "https://example.test/")
        assert links == ["https://example.test/a"]

    def test_empty_html(self):
        assert extract_links("", "https://example.test/") == []
        assert extract_links(None, "https://example.test/") == []

    def test_is_allowed_defaults_true_without_robots(self):
        assert is_allowed(None, "https://example.test/a") is True

    def test_is_allowed_honours_robots(self):
        from urllib.robotparser import RobotFileParser

        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /private"])
        assert is_allowed(parser, "https://example.test/private") is False
        assert is_allowed(parser, "https://example.test/public") is True

    def test_server_uses_the_shared_implementation(self):
        assert server_module.extract_links is extract_links
        assert server_module.load_robots is load_robots


# ==========================================================================
# api_intelligence
# ==========================================================================
class TestProxySniffer:
    def test_filters_static_assets(self):
        sniffer = ProxySniffer()
        for url in ("https://x.test/a.js", "https://x.test/a.css", "https://x.test/a.png"):
            sniffer.handle_request({"url": url, "method": "GET"})
        assert sniffer.get_summary() == []

    def test_records_api_traffic(self):
        sniffer = ProxySniffer()
        sniffer.handle_request({
            "url": "https://x.test/api/users", "method": "POST",
            "headers": {"X": "1"}, "post_data": '{"a":1}',
        })
        summary = sniffer.get_summary()
        assert len(summary) == 1
        assert summary[0]["method"] == "POST"
        assert summary[0]["path"] == "/api/users"
        assert summary[0]["host"] == "x.test"
        assert summary[0]["sample_request"] == '{"a":1}'

    def test_correlates_response_with_request(self):
        sniffer = ProxySniffer()
        sniffer.handle_request({"url": "https://x.test/api", "method": "GET"})
        sniffer.handle_response("https://x.test/api", {
            "headers": {}, "content_type": "application/json", "status": 200, "body": '{"ok":true}',
        })
        endpoint = sniffer.endpoints[0]
        assert endpoint.content_type == "application/json"
        assert endpoint.response_payload == '{"ok":true}'

    def test_response_without_request_is_ignored(self):
        sniffer = ProxySniffer()
        sniffer.handle_response("https://x.test/unknown", {"status": 200})
        assert sniffer.get_summary() == []


class TestSchemaGenerator:
    def test_json_schema_inference(self):
        schema = SchemaGenerator().generate_json_schema('{"a":1,"b":{"c":"x"}}')
        assert schema["type"] == "object"
        assert schema["properties"]["a"]["type"] == "integer"

    def test_openapi_spec_from_endpoints(self):
        sniffer = ProxySniffer()
        sniffer.handle_request({"url": "https://x.test/api/users", "method": "GET"})
        sniffer.handle_response("https://x.test/api/users", {
            "content_type": "application/json", "status": 200, "body": '{"id":1}',
        })
        spec = SchemaGenerator(service_title="T", version="2.0.0").generate_openapi_spec(
            sniffer.endpoints
        )
        assert spec["openapi"] == "3.0.0"
        assert spec["info"]["title"] == "T"
        assert spec["info"]["version"] == "2.0.0"
        assert "get" in spec["paths"]["/api/users"]
        assert spec["paths"]["/api/users"]["get"]["responses"]["200"]["content"]

    def test_document_requests_are_excluded_from_the_spec(self):
        """A crawl also records the page load; that is not an API endpoint."""
        sniffer = ProxySniffer()
        sniffer.handle_request({"url": "https://x.test/", "method": "GET"})
        sniffer.handle_response("https://x.test/", {
            "content_type": "text/html; charset=utf-8", "status": 200, "body": "<html></html>",
        })
        sniffer.handle_request({"url": "https://x.test/api/users", "method": "GET"})
        sniffer.handle_response("https://x.test/api/users", {
            "content_type": "application/json", "status": 200, "body": '{"id":1}',
        })
        generator = SchemaGenerator()
        spec = generator.generate_openapi_spec(sniffer.endpoints)
        assert set(spec["paths"]) == {"/api/users"}

    def test_documents_can_be_included_explicitly(self):
        sniffer = ProxySniffer()
        sniffer.handle_request({"url": "https://x.test/", "method": "GET"})
        sniffer.handle_response("https://x.test/", {
            "content_type": "text/html", "status": 200, "body": "<html></html>",
        })
        spec = SchemaGenerator().generate_openapi_spec(sniffer.endpoints, skip_documents=False)
        assert "/" in spec["paths"]

    def test_is_document_classifier(self):
        html = DiscoveredEndpoint("GET", "https://x.test/")
        html.content_type = "text/html"
        api = DiscoveredEndpoint("GET", "https://x.test/api")
        api.content_type = "application/json"
        assert SchemaGenerator.is_document(html) is True
        assert SchemaGenerator.is_document(api) is False

    def test_invalid_payload_degrades_gracefully(self):
        assert SchemaGenerator().generate_json_schema("{not json")["type"] == "string"


class TestBrowserPoolApiIntelligence:
    def test_pool_accepts_a_sniffer(self):
        sniffer = ProxySniffer()
        assert BrowserPool(api_sniffer=sniffer).api_sniffer is sniffer

    def test_pool_defaults_to_no_sniffer(self):
        assert BrowserPool().api_sniffer is None

    def test_attach_registers_request_and_response_listeners(self):
        class _Context:
            def __init__(self):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

        context = _Context()
        sniffer = ProxySniffer()
        assert run_async(attach_api_intelligence(context, sniffer)) is sniffer
        assert set(context.handlers) == {"request", "response"}

    def test_attached_request_handler_feeds_the_sniffer(self):
        class _Context:
            def __init__(self):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

        class _Request:
            url = "https://x.test/api/thing"
            method = "GET"
            headers = {"Accept": "application/json"}
            post_data = None

        context = _Context()
        sniffer = ProxySniffer()
        run_async(attach_api_intelligence(context, sniffer, capture_bodies=False))
        context.handlers["request"](_Request())
        assert len(sniffer.get_summary()) == 1

    def test_attached_response_handler_correlates(self):
        class _Context:
            def __init__(self):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

        class _Request:
            url = "https://x.test/api/thing"
            method = "GET"
            headers = {}
            post_data = None

        class _Response:
            url = "https://x.test/api/thing"
            status = 200
            headers = {"content-type": "application/json"}

            async def body(self):
                return b'{"ok":1}'

        context = _Context()
        sniffer = ProxySniffer()
        run_async(attach_api_intelligence(context, sniffer))
        context.handlers["request"](_Request())
        run_async(context.handlers["response"](_Response()))
        assert sniffer.endpoints[0].response_payload == '{"ok":1}'

    def test_hook_failures_never_propagate(self):
        class _Context:
            def __init__(self):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

        class _BadRequest:
            @property
            def url(self):
                raise RuntimeError("boom")

        context = _Context()
        run_async(attach_api_intelligence(context, ProxySniffer()))
        context.handlers["request"](_BadRequest())  # must not raise


class TestServerApiIntelligence:
    def test_crawl_hands_a_sniffer_to_the_browser_pool(self, client, monkeypatch):
        """The pool must receive a live ProxySniffer when API intel is on."""
        from byconn.tests.doubles import FakeBrowserPool

        monkeypatch.setattr(server_module, "API_INTELLIGENCE", True)
        seen = {}

        original_init = FakeBrowserPool.__init__

        def _spy(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            seen["api_sniffer"] = kwargs.get("api_sniffer")

        monkeypatch.setattr(FakeBrowserPool, "__init__", _spy)
        poll_job(client, _start(client))
        assert isinstance(seen.get("api_sniffer"), ProxySniffer)

    def test_crawl_passes_no_sniffer_when_disabled(self, client, monkeypatch):
        from byconn.tests.doubles import FakeBrowserPool

        monkeypatch.setattr(server_module, "API_INTELLIGENCE", False)
        seen = {}

        original_init = FakeBrowserPool.__init__

        def _spy(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            seen["api_sniffer"] = kwargs.get("api_sniffer")

        monkeypatch.setattr(FakeBrowserPool, "__init__", _spy)
        poll_job(client, _start(client))
        assert seen.get("api_sniffer") is None

    def test_crawl_with_no_discovered_endpoints_reports_zero(self, client, monkeypatch):
        monkeypatch.setattr(server_module, "API_INTELLIGENCE", True)
        job = poll_job(client, _start(client))
        assert job["endpoints_discovered"] == 0
        assert not any("discovered" in e for e in job["errors"])

    def test_discovered_apis_route_lists_persisted_endpoints(self, client):
        client.app.state.postgres.discovered.extend([
            {"method": "GET", "url": "https://x.test/a", "host": "x.test", "path": "/a"},
            {"method": "POST", "url": "https://y.test/b", "host": "y.test", "path": "/b"},
        ])
        body = client.get("/api/v1/apis").json()
        assert body["count"] == 2

    def test_discovered_apis_route_filters_by_host(self, client):
        client.app.state.postgres.discovered.extend([
            {"method": "GET", "url": "https://x.test/a", "host": "x.test", "path": "/a"},
            {"method": "GET", "url": "https://y.test/b", "host": "y.test", "path": "/b"},
        ])
        body = client.get("/api/v1/apis", params={"host": "x.test"}).json()
        assert body["count"] == 1
        assert body["endpoints"][0]["host"] == "x.test"

    def test_publish_generates_an_openapi_spec(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(server_module, "API_INTELLIGENCE", True)
        client.app.state.output_dir = str(tmp_path)
        sniffer = ProxySniffer()
        sniffer.handle_request({"url": "https://x.test/api/v1/items", "method": "GET"})
        sniffer.handle_response("https://x.test/api/v1/items", {
            "content_type": "application/json", "status": 200, "body": '{"items":[]}',
        })
        poll_job(client, _start(client))
        # Publish the sniffed surface for a synthetic job.
        crawl_job = server_module.CrawlJob(job_id="pub-test", url="https://x.test", max_depth=0)
        run_async(server_module.publish_discovered_apis(crawl_job, sniffer))
        assert crawl_job.endpoints_discovered == 1
        assert len(client.app.state.postgres.discovered) == 1
        spec_file = tmp_path / "openapi_pub-test.json"
        assert spec_file.exists()
        spec = json.loads(spec_file.read_text())
        assert "/api/v1/items" in spec["paths"]


# ==========================================================================
# visual agent
# ==========================================================================
NODES = [
    {"id": 0, "role": "searchbox", "text": "Search", "x": 100, "y": 50},
    {"id": 1, "role": "button", "text": "Go", "x": 200, "y": 50},
]


class TestNormaliseAction:
    def test_resolves_target_by_id(self):
        action = normalise_action({"type": "click", "id": 1}, NODES)
        assert action["x"] == 200 and action["element_id"] == 1

    def test_resolves_target_by_coordinates(self):
        action = normalise_action({"type": "click", "x": 100, "y": 50}, NODES)
        assert action["element_id"] == 0

    def test_unknown_id_falls_back_to_raw_coordinates(self):
        action = normalise_action({"type": "click", "id": 99, "x": 5, "y": 6}, NODES)
        assert action["x"] == 5 and action["y"] == 6

    def test_click_without_a_resolvable_target_stops(self):
        assert normalise_action({"type": "click"}, NODES)["type"] == "stop"

    def test_invalid_action_type_stops(self):
        assert normalise_action({"type": "explode"}, NODES)["type"] == "stop"

    def test_non_dict_stops(self):
        assert normalise_action("nope", NODES)["type"] == "stop"

    def test_type_action_keeps_value(self):
        action = normalise_action({"type": "type", "id": 0, "value": "hello"}, NODES)
        assert action["type"] == "type" and action["value"] == "hello"

    def test_stop_passes_through(self):
        assert normalise_action({"type": "stop"}, NODES) == {"type": "stop"}

    def test_scroll_is_allowed_without_coordinates(self):
        assert normalise_action({"type": "scroll", "delta": 500}, NODES)["type"] == "scroll"


class TestHeuristicPlanner:
    def test_types_into_a_search_field(self):
        action = run_async(HeuristicPlanner().plan("find cats", NODES, "https://x.test"))
        assert action["type"] == "type"
        assert action["element_id"] == 0

    def test_clicks_submit_when_no_input(self):
        nodes = [{"id": 0, "role": "button", "text": "Submit", "x": 10, "y": 10}]
        action = run_async(HeuristicPlanner().plan("go", nodes, ""))
        assert action["type"] == "click" and action["element_id"] == 0

    def test_stops_with_no_nodes(self):
        assert run_async(HeuristicPlanner().plan("anything", [], ""))["type"] == "stop"

    def test_typed_value_override(self):
        action = run_async(
            HeuristicPlanner(typed_value="preset").plan("goal", NODES, "")
        )
        assert action["value"] == "preset"


class TestLLMPlanner:
    def test_reports_availability_from_the_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert LLMPlanner().is_available is False

    def test_unavailable_planner_stops(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert run_async(LLMPlanner().plan("goal", NODES, ""))["type"] == "stop"

    def test_action_parsed_from_the_model(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        planner = LLMPlanner()
        monkeypatch.setattr(
            planner.extractor, "_call_with_retries",
            lambda prompt, image=None: _async_return('{"type":"click","id":1}'),
        )
        action = run_async(planner.plan("goal", NODES, "https://x.test"))
        assert action["type"] == "click" and action["element_id"] == 1

    def test_model_failure_stops(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        planner = LLMPlanner()
        monkeypatch.setattr(
            planner.extractor, "_call_with_retries",
            lambda prompt, image=None: _async_return(None),
        )
        assert run_async(planner.plan("goal", NODES, ""))["type"] == "stop"


async def _async_return(value):
    return value


class TestVisionFlow:
    """The screenshot must reach the model, not just be captured."""

    def test_heuristic_planner_accepts_a_screenshot(self):
        """Interface parity: both planners take the same arguments."""
        action = run_async(HeuristicPlanner().plan("goal", NODES, "https://x.test", b"jpeg"))
        assert action["type"] == "type"

    def test_llm_planner_forwards_the_screenshot(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        planner = LLMPlanner()
        captured = {}

        async def _capture(prompt, image=None):
            captured["prompt"] = prompt
            captured["image"] = image
            return '{"type":"click","id":1}'

        monkeypatch.setattr(planner.extractor, "_call_with_retries", _capture)
        run_async(planner.plan("goal", NODES, "https://x.test", b"fake-jpeg-bytes"))
        assert captured["image"] == b"fake-jpeg-bytes"
        assert "screenshot of the current viewport" in captured["prompt"]

    def test_no_screenshot_uses_the_dom_only_prompt(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        planner = LLMPlanner()
        captured = {}

        async def _capture(prompt, image=None):
            captured["prompt"] = prompt
            captured["image"] = image
            return '{"type":"stop"}'

        monkeypatch.setattr(planner.extractor, "_call_with_retries", _capture)
        run_async(planner.plan("goal", NODES, "https://x.test"))
        assert captured["image"] is None
        assert "No screenshot available" in captured["prompt"]

    def test_oversized_screenshot_is_dropped(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        planner = LLMPlanner()
        captured = {}

        async def _capture(prompt, image=None):
            captured["image"] = image
            return '{"type":"stop"}'

        monkeypatch.setattr(planner.extractor, "_call_with_retries", _capture)
        oversized = b"x" * (MAX_IMAGE_BYTES + 1)
        run_async(planner.plan("goal", NODES, "", oversized))
        assert captured["image"] is None, "oversized images must not be sent"

    def test_agent_passes_the_screenshot_to_the_planner(self):
        received = {}

        class _Recorder:
            def __init__(self):
                self.calls = 0

            async def plan(self, goal, nodes, url="", screenshot=None):
                self.calls += 1
                received["screenshot"] = screenshot
                received["url"] = url
                return {"type": "stop"} if self.calls > 1 else {"type": "hover", "x": 1, "y": 2}

        page = _StubPage()
        agent = VisualBrowserAgent(page, planner=_Recorder(), step_delay=0)
        run_async(agent.execute_task("goal", max_steps=3))
        assert received["screenshot"], "agent must forward the captured screenshot"
        assert received["url"] == page.url

    def test_agent_captures_a_compact_jpeg(self):
        class _Stop:
            async def plan(self, goal, nodes, url="", screenshot=None):
                return {"type": "stop"}

        page = _StubPage()
        run_async(VisualBrowserAgent(page, planner=_Stop(), step_delay=0).execute_task("g", max_steps=1))
        # JPEG keeps the vision payload small enough to be affordable.
        assert page.shot_kwargs == {"type": "jpeg", "quality": 60}

    def test_legacy_planner_without_screenshot_still_works(self):
        """A planner predating the screenshot argument must not break the agent."""
        class _Legacy:
            def __init__(self):
                self.calls = 0

            async def plan(self, goal, nodes, url=""):
                self.calls += 1
                return {"type": "stop"} if self.calls > 1 else {"type": "hover", "x": 1, "y": 2}

        agent = VisualBrowserAgent(_StubPage(), planner=_Legacy(), step_delay=0)
        assert run_async(agent.execute_task("goal", max_steps=3)) is True

    def test_image_data_url_encoding(self):
        from byconn.pipeline.llm_extractor import LLMExtractor

        url = LLMExtractor._image_data_url(b"abc", "image/jpeg")
        assert url.startswith("data:image/jpeg;base64,")
        assert url.endswith("YWJj")  # base64("abc")


class TestBuildPlanner:
    def test_falls_back_to_heuristic_without_a_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert isinstance(build_planner(), HeuristicPlanner)

    def test_uses_the_llm_when_a_key_exists(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert isinstance(build_planner(), LLMPlanner)

    def test_heuristic_can_be_forced(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert isinstance(build_planner(prefer_llm=False), HeuristicPlanner)


class _StubMouse:
    def __init__(self):
        self.clicks, self.moves, self.wheels = [], [], []

    async def click(self, x, y):
        self.clicks.append((x, y))

    async def move(self, x, y):
        self.moves.append((x, y))

    async def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class _StubKeyboard:
    def __init__(self):
        self.typed, self.pressed = [], []

    async def type(self, value):
        self.typed.append(value)

    async def press(self, key):
        self.pressed.append(key)


class _StubPage:
    def __init__(self):
        self.url = "https://x.test/"
        self.mouse = _StubMouse()
        self.keyboard = _StubKeyboard()
        self.screenshots = 0
        self.shot_kwargs = None

    async def screenshot(self, type="png", quality=None):
        self.screenshots += 1
        self.shot_kwargs = {"type": type, "quality": quality}
        return b"jpeg-bytes"

    async def goto(self, url, wait_until=None):
        self.url = url


class TestVisualBrowserAgent:
    def _agent(self, planner, page=None):
        page = page or _StubPage()
        return VisualBrowserAgent(page, planner=planner, step_delay=0)

    def test_executes_until_the_planner_stops(self):
        class _StopAfterType:
            def __init__(self):
                self.calls = 0

            async def plan(self, goal, nodes, url="", screenshot=None):
                self.calls += 1
                if self.calls == 1:
                    return {"type": "type", "x": 10, "y": 20, "value": "hi"}
                return {"type": "stop"}

        planner = _StopAfterType()
        page = _StubPage()
        agent = self._agent(planner, page)
        assert run_async(agent.execute_task("goal", max_steps=5)) is True
        assert page.mouse.clicks == [(10, 20)]
        assert page.keyboard.typed == ["hi"]
        assert len(agent.history) == 2

    def test_returns_false_when_steps_run_out(self):
        class _NeverStop:
            async def plan(self, goal, nodes, url="", screenshot=None):
                return {"type": "hover", "x": 1, "y": 2}

        page = _StubPage()
        agent = self._agent(_NeverStop(), page)
        assert run_async(agent.execute_task("goal", max_steps=3)) is False
        assert len(agent.history) == 3

    def test_performs_a_click(self):
        class _Click:
            def __init__(self):
                self.calls = 0

            async def plan(self, goal, nodes, url="", screenshot=None):
                self.calls += 1
                return {"type": "click", "x": 7, "y": 8} if self.calls == 1 else {"type": "stop"}

        page = _StubPage()
        run_async(self._agent(_Click(), page).execute_task("g", max_steps=3))
        assert page.mouse.clicks == [(7, 8)]

    def test_performs_a_scroll(self):
        class _Scroll:
            def __init__(self):
                self.calls = 0

            async def plan(self, goal, nodes, url="", screenshot=None):
                self.calls += 1
                return {"type": "scroll", "delta": 300} if self.calls == 1 else {"type": "stop"}

        page = _StubPage()
        run_async(self._agent(_Scroll(), page).execute_task("g", max_steps=3))
        assert page.mouse.wheels == [(0, 300)]

    def test_malformed_action_ends_the_task(self):
        class _Bad:
            async def plan(self, goal, nodes, url="", screenshot=None):
                return {"type": "click"}  # no coordinates

        agent = self._agent(_Bad())
        assert run_async(agent.execute_task("g", max_steps=3)) is False

    def test_planner_failure_ends_the_task(self):
        class _Broken:
            async def plan(self, goal, nodes, url="", screenshot=None):
                raise RuntimeError("planner down")

        agent = self._agent(_Broken())
        assert run_async(agent.execute_task("g", max_steps=3)) is True  # degrades to stop

    def test_screenshot_failure_is_not_fatal(self):
        class _NoShot(_StubPage):
            async def screenshot(self, type="png"):
                raise RuntimeError("no display")

        class _Stop:
            async def plan(self, goal, nodes, url="", screenshot=None):
                return {"type": "stop"}

        agent = self._agent(_Stop(), _NoShot())
        assert run_async(agent.execute_task("g", max_steps=2)) is True

    def test_heuristic_planner_drives_a_real_agent(self):
        page = _StubPage()
        agent = VisualBrowserAgent(page, planner=HeuristicPlanner(), step_delay=0)
        agent.dom_parser.get_interactables = lambda _p: _async_nodes(NODES)
        assert run_async(agent.execute_task("search for cats", max_steps=1)) is False
        assert page.keyboard.typed  # typed into the search box


async def _async_nodes(nodes):
    return nodes


# ==========================================================================
# deduplication
# ==========================================================================
class TestDeduplication:
    BODY = (
        "<p>This domain is for use in documentation examples without needing "
        "permission. Avoid use in operations.</p>"
    )

    def test_first_document_is_not_a_duplicate(self):
        cleaner = DataCleaner()
        assert cleaner.is_duplicate(self.BODY) is False
        assert cleaner.mark_seen(self.BODY) is True
        # Once recorded it is, by definition, found by a duplicate lookup.
        assert cleaner.is_duplicate(self.BODY) is True

    def test_identical_document_is_a_duplicate(self):
        cleaner = DataCleaner()
        cleaner.mark_seen(self.BODY)
        assert cleaner.is_duplicate(self.BODY) is True
        assert cleaner.mark_seen(self.BODY) is False

    def test_different_document_is_not_a_duplicate(self):
        cleaner = DataCleaner()
        cleaner.mark_seen(self.BODY)
        other = "<p>Completely unrelated content about distributed systems and queues.</p>"
        assert cleaner.is_duplicate(other) is False

    def test_short_text_never_counts_as_duplicate(self):
        cleaner = DataCleaner()
        cleaner.mark_seen("short")
        assert cleaner.is_duplicate("short") is False

    def test_reset_forgets_everything(self):
        cleaner = DataCleaner()
        cleaner.mark_seen(self.BODY)
        cleaner.reset_dedup()
        assert cleaner.is_duplicate(self.BODY) is False

    def test_capacity_is_bounded(self):
        cleaner = DataCleaner(dedup_capacity=5)
        for i in range(20):
            cleaner.mark_seen(
                f"<p>Document number {i} with enough words to exceed the minimum length "
                f"threshold for shingling.</p>"
            )
        assert len(cleaner._seen_shingles) == 5

    def test_deduplicate_blocks_drops_repeats(self):
        cleaner = DataCleaner()
        blocks = cleaner.deduplicate_blocks([
            "Unique heading",
            " ".join(["repeated boilerplate line"] * 12),
            " ".join(["repeated boilerplate line"] * 12),
        ])
        assert len(blocks) == 2

    def test_deduplicate_blocks_drops_blanks(self):
        cleaner = DataCleaner()
        assert cleaner.deduplicate_blocks(["a", "", "   ", "b"]) == ["a", "b"]

    def test_html_to_unique_markdown_returns_none_for_duplicates(self):
        cleaner = DataCleaner()
        html = "<p>" + ("A reasonably long sentence that repeats across pages. " * 3) + "</p>"
        assert cleaner.html_to_unique_markdown(html) is not None
        assert cleaner.html_to_unique_markdown(html) is None

    def test_html_to_unique_markdown_returns_none_when_empty(self):
        assert DataCleaner().html_to_unique_markdown("<body></body>") is None

    def test_html_to_markdown_still_works_standalone(self):
        markdown = DataCleaner().html_to_markdown(
            "<h1>T</h1><p>Body</p>"
        )
        assert "# T" in markdown and "Body" in markdown

    def test_server_skips_duplicate_pages(self, client, monkeypatch):
        """A second crawl of the same content must not store or embed it again."""
        monkeypatch.setattr(server_module, "DEDUPLICATE_PAGES", True)
        postgres = client.app.state.postgres
        postgres.pages.clear()
        job = poll_job(client, _start(client))
        assert job["status"] == "succeeded"
        assert job["pages_crawled"] == 1
        assert job["duplicates_skipped"] == 0
        assert len(postgres.pages) == 1

    def test_job_reports_duplicate_counter(self, client):
        job = poll_job(client, _start(client))
        assert "duplicates_skipped" in job
        assert "endpoints_discovered" in job


# ==========================================================================
# agent endpoint
# ==========================================================================
def _start(client, url="https://example.com/", depth=0):
    from byconn.tests.doubles import start_crawl

    return start_crawl(client, url, depth)


class TestAgentEndpoint:
    @pytest.mark.parametrize("payload", [
        {"url": "ftp://example.com", "task": "click"},
        {"url": "https://example.com", "task": ""},
        {"url": "https://example.com"},
        {},
    ])
    def test_rejects_bad_requests(self, client, payload):
        assert client.post("/api/v1/act", json=payload).status_code == 422

    def test_rejects_out_of_range_steps(self, client):
        assert client.post("/api/v1/act", json={
            "url": "https://example.com", "task": "t", "max_steps": 0,
        }).status_code == 422

    def test_accepts_a_valid_task(self, client):
        response = client.post("/api/v1/act", json={
            "url": "https://example.com", "task": "find the login button", "max_steps": 3,
        })
        assert response.status_code == 202
        assert len(response.json()["job_id"]) == 32

    def test_unknown_agent_job_returns_404(self, client):
        assert client.get("/api/v1/act/nope").status_code == 404

    def test_agent_job_is_tracked(self, client):
        response = client.post("/api/v1/act", json={
            "url": "https://example.com", "task": "t", "max_steps": 2,
        })
        job_id = response.json()["job_id"]
        body = client.get(f"/api/v1/act/{job_id}").json()
        assert body["task"] == "t"
        assert body["status"] in {"queued", "running", "succeeded", "failed"}
        assert body["max_steps"] == 2

    def test_openapi_documents_the_agent_routes(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/act" in paths
        assert "/api/v1/act/{job_id}" in paths
        assert "/api/v1/apis" in paths
