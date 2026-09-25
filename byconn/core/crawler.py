import asyncio
import logging
import psutil
from typing import Callable, Coroutine, Any, Optional
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
        max_memory_percent: float = 85.0
    ):
        self.request_queue = request_queue
        self.browser_pool = browser_pool
        self.session_pool = session_pool
        self.concurrency = concurrency
        self.max_memory_percent = max_memory_percent
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

            # Provision context for request
            context = await self.browser_pool.new_context()
            page = await context.new_page()
            
            try:
                logger.info(f"Worker processing request: {req.url}")
                await page.goto(req.url, wait_until="domcontentloaded")
                # Execute user-defined page handler hook
                await handler_func(req, page)
                await self.request_queue.complete(req)
            except Exception as e:
                logger.exception(f"Exception encountered during crawl of {req.url}")
                await self.request_queue.fail(req)
            finally:
                await page.close()
                await context.close()

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
class ByconnCrawler:
    """
    High-level wrapper over BaseCrawler.
    Accepts start_urls and wires BrowserPool, SessionPool, and RequestQueue together
    so callers can import and run with minimal boilerplate.

    Usage:
        crawler = ByconnCrawler(concurrency=10, stealth_mode=True)
        await crawler.run(start_urls=["https://example.com"])
    """
    def __init__(
        self,
        concurrency: int = 5,
        stealth_mode: bool = True,
        proxy_rotation: bool = False,
        proxy_list: list = None,
        max_memory_percent: float = 85.0
    ):
        self.concurrency = concurrency
        self.stealth_mode = stealth_mode
        self.proxy_list = proxy_list or []
        self.max_memory_percent = max_memory_percent
        self._results: list = []

    async def run(
        self,
        start_urls: list,
        handler=None,
        max_depth: int = 1
    ):
        """
        Launches a crawl across start_urls.

        Args:
            start_urls: List of seed URLs to begin crawling.
            handler: Optional async callback(request, page) -> None.
                     Defaults to a simple text-extraction handler.
            max_depth: Number of link levels to follow from each seed URL.
        """
        from .browser_pool import BrowserPool
        from .request_queue import RequestQueue, CrawlRequest
        from .session_pool import SessionPool

        browser_pool = BrowserPool(
            proxy_list=self.proxy_list,
            headless=True
        )
        session_pool = SessionPool()
        request_queue = RequestQueue()

        # Seed the queue with initial URLs
        for url in start_urls:
            await request_queue.add(CrawlRequest(url=url))

        async def _default_handler(req: CrawlRequest, page):
            title = await page.title()
            content = await page.inner_text("body")
            self._results.append({
                "url": req.url,
                "title": title,
                "content": content[:2000]  # Truncate for memory safety
            })
            logger.info(f"ByconnCrawler extracted: {title} ({req.url})")

        crawler = BaseCrawler(
            request_queue=request_queue,
            browser_pool=browser_pool,
            session_pool=session_pool,
            concurrency=self.concurrency,
            max_memory_percent=self.max_memory_percent
        )

        await crawler.run(handler_func=handler or _default_handler)
        await crawler.wait_for_completion()
        return self._results

    def get_results(self) -> list:
        """Returns all scraped results collected during the crawl."""
        return self._results
