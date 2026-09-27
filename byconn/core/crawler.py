import asyncio
import logging
import psutil
from typing import Callable, Coroutine, Any
from .browser_pool import BrowserPool
from .request_queue import RequestQueue, CrawlRequest
from .session_pool import SessionPool

logger = logging.getLogger("byconnx.core.crawler")

class BaseCrawler:
    """
    Main orchestration engine managing page visits, request queue processing,
    and adaptive autoscaling based on system resources (AutoscaledPool concept).
    """
    def __init__(
        self,
        request_queue: RequestQueue,
        browser_pool: BrowserPool,
        session_pool: SessionPool,
        concurrency: int = 5,
        max_memory_percent: float = 85.0,
        navigation_timeout: int = 30000,
        handler_timeout: float = 150.0,
    ):
        self.request_queue = request_queue
        self.browser_pool = browser_pool
        self.session_pool = session_pool
        self.concurrency = concurrency
        self.max_memory_percent = max_memory_percent
        # Navigation must be bounded: without this a single unresponsive host
        # blocks its worker for the lifetime of the crawl.
        self.navigation_timeout = navigation_timeout
        # The handler is bounded too, and the queue's lock must outlast
        # navigation plus handler: otherwise a slow page is reclaimed and
        # handed to a second worker while the first is still processing it.
        self.handler_timeout = handler_timeout
        min_lock = navigation_timeout / 1000 + handler_timeout + 30
        if getattr(request_queue, "lock_duration_sec", min_lock) < min_lock:
            logger.info(
                "Raising request lock duration from %ss to %ss to cover "
                "navigation and handler timeouts.",
                request_queue.lock_duration_sec, int(min_lock),
            )
            request_queue.lock_duration_sec = int(min_lock)
        self.running = False
        self.workers = []

    async def run(self, handler_func: Callable[[CrawlRequest, Any], Coroutine[Any, Any, None]]):
        """Starts the crawling processing loops across workers."""
        self.running = True
        await self.browser_pool.initialize()

        logger.info(f"Starting BaseCrawler fleet with base concurrency of {self.concurrency}")
        self.workers = [asyncio.create_task(self._worker_loop(handler_func)) for _ in range(self.concurrency)]

    async def wait_for_completion(self):
        """Waits until the request queue is completely drained and no tasks are in progress."""
        while self.running:
            if await self.request_queue.is_finished():
                logger.info("Request queue is empty and all tasks completed. Shutting down...")
                await self.stop()
                break
            await asyncio.sleep(1)

        # Wait for workers to finish current loops and exit
        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)

    async def _worker_loop(self, handler_func: Callable[[CrawlRequest, Any], Coroutine[Any, Any, None]]):
        """Dedicated worker executing page scrapes from the request queue."""
        while self.running:
            # Check system health constraints prior to scraping
            if not self._check_resource_limits():
                logger.warning("Resource limits exceeded. Throttling current worker thread execution...")
                await asyncio.sleep(5)
                continue

            req = await self.request_queue.get_next()
            if not req:
                # No tasks right now; wait before checking again
                await asyncio.sleep(2)
                continue

            # Everything after get_next() is inside the try: if context or
            # page creation raised outside it, the worker task died with the
            # request still locked, and once every worker had died
            # wait_for_completion() polled forever.
            context = None
            page = None
            try:
                context = await self.browser_pool.new_context()
                page = await context.new_page()
                logger.info(f"Worker processing request: {req.url}")
                response = await page.goto(
                    req.url,
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout,
                )
                if response is not None:
                    req.payload["status_code"] = response.status
                # Execute user-defined page handler hook. A timeout here is
                # not retried: the handler has already done (and counted)
                # expensive work, and re-running it would repeat LLM calls.
                try:
                    await asyncio.wait_for(handler_func(req, page), timeout=self.handler_timeout)
                except asyncio.TimeoutError:
                    logger.error(
                        "Handler for %s exceeded %.0fs; dropping it without retry.",
                        req.url, self.handler_timeout,
                    )
                await self.request_queue.complete(req)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"Exception encountered during crawl of {req.url}")
                await self.request_queue.fail(req)
            finally:
                for resource in (page, context):
                    if resource is None:
                        continue
                    try:
                        await resource.close()
                    except Exception as exc:
                        logger.debug("Failed to close browser resource: %s", exc)

    def _check_resource_limits(self) -> bool:
        """Adaptive autoscaling: returns False if RAM limits are reached."""
        mem = psutil.virtual_memory()
        if mem.percent > self.max_memory_percent:
            logger.error(f"High RAM usage detected: {mem.percent}%. Throttling workers.")
            return False
        return True

    async def stop(self):
        """Safely stops all crawling workers."""
        self.running = False
        for worker in self.workers:
            worker.cancel()
        await self.browser_pool.close()
        logger.info("Crawler instances shut down.")
