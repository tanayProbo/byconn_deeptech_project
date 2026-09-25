"""Tests for byconn.core: request queue, session pool, browser pool helpers.

No live network or browser is required.
"""

import time

import pytest

from byconn.core.browser_pool import BrowserPool
from byconn.core.crawler import BaseCrawler
from byconn.core.request_queue import CrawlRequest, RequestQueue
from byconn.core.session_pool import ScrapingSession, SessionPool

from byconn.tests.doubles import FakeBrowserPool, FakePage, run_async


# ==========================================================================
# CrawlRequest
# ==========================================================================
class TestCrawlRequest:
    def test_defaults(self):
        request = CrawlRequest("https://example.com/")
        assert request.depth == 0
        assert request.max_depth == 3
        assert request.payload == {}
        assert request.lock_time is None

    def test_unique_key_is_the_url(self):
        assert CrawlRequest("https://example.com/a").unique_key == "https://example.com/a"

    def test_whitespace_in_url_is_preserved_but_key_is_raw(self):
        # Dedup keys on the exact string supplied; callers normalise upstream.
        request = CrawlRequest(" https://example.com/a ")
        assert request.unique_key == " https://example.com/a "


# ==========================================================================
# RequestQueue
# ==========================================================================
class TestRequestQueue:
    def test_add_and_drain(self):
        async def scenario():
            queue = RequestQueue()
            assert await queue.add(CrawlRequest("https://a.test")) is True
            assert await queue.is_finished() is False
            request = await queue.get_next()
            assert request is not None and request.url == "https://a.test"
            await queue.complete(request)
            assert await queue.is_finished() is True

        run_async(scenario())

    def test_duplicate_urls_are_rejected(self):
        async def scenario():
            queue = RequestQueue()
            assert await queue.add(CrawlRequest("https://a.test")) is True
            assert await queue.add(CrawlRequest("https://a.test")) is False

        run_async(scenario())

    def test_depth_beyond_max_is_rejected(self):
        async def scenario():
            queue = RequestQueue()
            # depth 2 exceeds max_depth 1
            assert await queue.add(CrawlRequest("https://a.test", depth=2, max_depth=1)) is False
            assert await queue.add(CrawlRequest("https://b.test", depth=1, max_depth=1)) is True

        run_async(scenario())

    def test_get_next_returns_none_when_empty(self):
        async def scenario():
            assert await RequestQueue().get_next() is None

        run_async(scenario())

    def test_get_next_marks_request_in_progress(self):
        async def scenario():
            queue = RequestQueue()
            await queue.add(CrawlRequest("https://a.test"))
            request = await queue.get_next()
            assert request.unique_key in queue.in_progress
            assert request.lock_time is not None
            # in_progress means the crawl is not finished yet.
            assert await queue.is_finished() is False

        run_async(scenario())

    def test_complete_releases_the_lock(self):
        async def scenario():
            queue = RequestQueue()
            await queue.add(CrawlRequest("https://a.test"))
            request = await queue.get_next()
            await queue.complete(request)
            assert request.unique_key not in queue.in_progress

        run_async(scenario())

    def test_fail_requeues_until_retry_limit(self):
        async def scenario():
            queue = RequestQueue()
            await queue.add(CrawlRequest("https://a.test"))
            for attempt in range(3):
                request = await queue.get_next()
                assert request is not None, f"expected a re-queued request on attempt {attempt}"
                await queue.fail(request)
                assert request.payload["retries"] == attempt + 1
            # Fourth failure exhausts the retry budget and drops the request.
            request = await queue.get_next()
            assert request is not None
            await queue.fail(request)
            assert await queue.get_next() is None
            assert await queue.is_finished() is True

        run_async(scenario())

    def test_failed_url_can_be_requeued(self):
        async def scenario():
            queue = RequestQueue()
            await queue.add(CrawlRequest("https://a.test"))
            request = await queue.get_next()
            await queue.fail(request)
            # The dedup key is released so a retry can re-enter the queue.
            assert await queue.add(CrawlRequest("https://a.test")) is True

        run_async(scenario())

    def test_expired_lock_is_reclaimed(self):
        async def scenario():
            queue = RequestQueue(lock_duration_sec=1)
            await queue.add(CrawlRequest("https://stalled.test"))
            request = await queue.get_next()
            assert request.lock_time is not None
            # Simulate a worker that died holding the item.
            request.lock_time = time.time() - 10
            assert await queue.is_finished() is False
            reclaimed = await queue.get_next()
            assert reclaimed is not None
            assert reclaimed.url == "https://stalled.test"

        run_async(scenario())

    def test_unexpired_lock_is_not_reclaimed(self):
        async def scenario():
            queue = RequestQueue(lock_duration_sec=300)
            await queue.add(CrawlRequest("https://busy.test"))
            await queue.get_next()
            # Still locked, so nothing new is handed out.
            assert await queue.get_next() is None

        run_async(scenario())

    def test_is_finished_requires_both_empty_and_idle(self):
        async def scenario():
            queue = RequestQueue()
            assert await queue.is_finished() is True
            await queue.add(CrawlRequest("https://a.test"))
            assert await queue.is_finished() is False

        run_async(scenario())


# ==========================================================================
# SessionPool
# ==========================================================================
class TestSessionPool:
    def test_new_session_defaults_to_perfect_score(self):
        assert ScrapingSession("s1").score == 1.0

    def test_score_reflects_success_ratio(self):
        session = ScrapingSession("s1")
        session.record_success()
        session.record_success()
        session.record_error()
        assert session.score == pytest.approx(2 / 3)

    def test_get_session_reuses_existing(self):
        pool = SessionPool()
        first = pool.get_session("s1")
        assert pool.get_session("s1") is first

    def test_unhealthy_session_is_discarded(self):
        pool = SessionPool(session_max_errors=3)
        first = pool.get_session("s1")
        for _ in range(3):
            first.record_error()
        second = pool.get_session("s1")
        assert second is not first
        assert pool.sessions["s1"] is second

    def test_lowest_score_session_is_evicted_at_capacity(self):
        pool = SessionPool(max_sessions=2)
        pool.get_session("good")
        weak = pool.get_session("weak")
        weak.record_error()
        weak.record_error()
        assert pool.get_session("good").score == 1.0
        pool.get_session("third")
        assert "weak" not in pool.sessions
        assert set(pool.sessions) == {"good", "third"}

    def test_update_session_persists_cookies_and_headers(self):
        pool = SessionPool()
        pool.get_session("s1")
        pool.update_session("s1", cookies=[{"name": "a"}], headers={"X": "1"})
        assert pool.sessions["s1"].cookies == [{"name": "a"}]
        assert pool.sessions["s1"].headers == {"X": "1"}

    def test_update_session_for_unknown_id_is_a_noop(self):
        pool = SessionPool()
        pool.update_session("missing", cookies=[], headers={})  # must not raise
        assert "missing" not in pool.sessions


# ==========================================================================
# BrowserPool (no browser launched)
# ==========================================================================
class TestBrowserPoolHelpers:
    def test_fingerprint_has_consistent_shape(self):
        pool = BrowserPool()
        fingerprint = pool._generate_fingerprint()
        assert set(fingerprint) == {
            "user_agent", "viewport", "device_scale_factor",
            "is_mobile", "has_touch", "locale", "timezone_id",
        }
        assert "Mozilla/5.0" in fingerprint["user_agent"]
        assert fingerprint["viewport"] == {"width": 1920, "height": 1080}

    def test_fingerprint_varies_between_calls(self):
        pool = BrowserPool()
        agents = {pool._generate_fingerprint()["user_agent"] for _ in range(20)}
        assert len(agents) > 1, "user agent should rotate to look human"

    def test_no_proxy_configured_returns_none(self):
        assert BrowserPool()._get_random_proxy() is None

    def test_proxy_is_selected_from_the_list(self):
        pool = BrowserPool(proxy_list=[{"server": "http://p1:8080", "username": "u", "password": "p"}])
        assert pool._get_random_proxy() == {
            "server": "http://p1:8080", "username": "u", "password": "p",
        }

    def test_proxy_defaults_missing_credentials_to_empty(self):
        pool = BrowserPool(proxy_list=[{"server": "http://p1:8080"}])
        assert pool._get_random_proxy()["username"] == ""


# ==========================================================================
# CLI surface (console script + python -m byconn.main)
# ==========================================================================
class TestCliParser:
    """The documented invocations must all parse; argparse must not clobber."""

    def _parse(self, argv):
        from byconn.main import build_parser

        return build_parser().parse_args(argv)

    def test_documented_url_form(self):
        """`python -m byconn.main --url <URL> --depth 1` (documented form)."""
        args = self._parse(["--url", "https://example.com", "--depth", "1"])
        assert args.url == "https://example.com"
        assert args.depth == 1

    def test_url_form_with_all_options(self):
        args = self._parse([
            "--url", "https://example.com", "--depth", "3",
            "--concurrency", "5", "--output", "out.json",
        ])
        assert (args.depth, args.concurrency, args.output) == (3, 5, "out.json")

    def test_legacy_subcommand_form(self):
        """`byconn crawl <URL>` must keep working."""
        args = self._parse(["crawl", "https://example.com", "--depth", "2"])
        assert args.url == "https://example.com"
        assert args.depth == 2

    def test_subcommand_url_is_positional(self):
        args = self._parse(["crawl", "https://example.com"])
        assert args.url == "https://example.com"

    def test_subcommand_does_not_clobber_earlier_options(self):
        """SUPPRESS keeps a pre-subcommand value from being reset."""
        args = self._parse(["--depth", "4", "crawl", "https://example.com"])
        assert args.depth == 4, "subcommand default overwrote the earlier value"

    def test_defaults_without_any_arguments(self):
        args = self._parse(["--url", "https://example.com"])
        assert args.depth == 1
        assert args.concurrency == 2
        assert args.output == "byconn_results.json"

    def test_subcommand_may_be_omitted(self):
        args = self._parse(["--url", "https://example.com"])
        assert getattr(args, "command", None) is None

    def test_missing_url_is_an_error(self, capsys):
        from byconn.main import main

        with pytest.raises(SystemExit) as exc:
            main(["--depth", "1"])
        assert exc.value.code == 2
        assert "URL is required" in capsys.readouterr().err

    def test_console_script_entry_point_exists(self):
        import importlib

        assert callable(importlib.import_module("byconn.main").cli)
        assert callable(importlib.import_module("server").main)


# ==========================================================================
# BaseCrawler
# ==========================================================================
class TestBaseCrawlerResourceLimits:
    def _crawler(self):
        from byconn.core.session_pool import SessionPool
        return BaseCrawler(
            request_queue=RequestQueue(),
            browser_pool=FakeBrowserPool(),
            session_pool=SessionPool(),
            max_memory_percent=90.0,
        )

    def test_allows_work_below_threshold(self, monkeypatch):
        class _Mem:
            percent = 10.0
        monkeypatch.setattr("psutil.virtual_memory", lambda: _Mem())
        assert self._crawler()._check_resource_limits() is True

    def test_throttles_above_threshold(self, monkeypatch):
        class _Mem:
            percent = 99.0
        monkeypatch.setattr("psutil.virtual_memory", lambda: _Mem())
        assert self._crawler()._check_resource_limits() is False

    def test_allows_work_exactly_at_threshold(self, monkeypatch):
        # The crawler throttles only when usage *exceeds* the limit, so the
        # boundary value itself is still allowed to proceed.
        class _Mem:
            percent = 90.0
        monkeypatch.setattr("psutil.virtual_memory", lambda: _Mem())
        assert self._crawler()._check_resource_limits() is True

    def test_initial_state(self):
        crawler = self._crawler()
        assert crawler.running is False
        assert crawler.workers == []


class TestBaseCrawlerWorkerLoop:
    def test_handler_is_invoked_and_request_completed(self, monkeypatch):
        """The worker navigates, calls the handler, then completes the request."""

        seen = {}

        class _Context:
            async def new_page(self):
                return FakePage("https://a.test/")

            async def close(self):
                return None

        class _Pool:
            def __init__(self):
                self.closed = False

            async def initialize(self):
                return None

            async def new_context(self):
                return _Context()

            async def close(self):
                self.closed = True

        class _Page(FakePage):
            async def goto(self, url, wait_until=None, timeout=None):
                seen["goto"] = (url, wait_until, timeout)

            async def close(self):
                return None

        class _Ctx2:
            async def new_page(self):
                return _Page("https://a.test/")

            async def close(self):
                return None

        class _Pool2(_Pool):
            async def new_context(self):
                return _Ctx2()

        async def scenario():
            from byconn.core.session_pool import SessionPool

            queue = RequestQueue()
            await queue.add(CrawlRequest("https://a.test/"))
            pool = _Pool2()
            crawler = BaseCrawler(
                request_queue=queue, browser_pool=pool,
                session_pool=SessionPool(), concurrency=1,
            )
            handled = []

            async def handler(request, page):
                handled.append((request.url, await page.title()))

            await crawler.run(handler)
            # wait_for_completion drives the loop; stop() cancels workers.
            await crawler.wait_for_completion()
            return handled, pool

        handled, pool = run_async(scenario())
        assert handled and handled[0][0] == "https://a.test/"
        url, wait_until, timeout = seen.get("goto", (None, None, None))
        assert url == "https://a.test/"
        assert wait_until == "domcontentloaded"
        # Navigation must be bounded, or one unresponsive host hangs a worker.
        assert timeout == 30000
        assert pool.closed is True

    def test_navigation_timeout_is_configurable(self):
        from byconn.core.session_pool import SessionPool

        crawler = BaseCrawler(
            request_queue=RequestQueue(),
            browser_pool=FakeBrowserPool(),
            session_pool=SessionPool(),
            navigation_timeout=5000,
        )
        assert crawler.navigation_timeout == 5000
