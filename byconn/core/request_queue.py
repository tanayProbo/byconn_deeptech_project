import time
import asyncio
import logging
from typing import Dict, Any, Optional, Set

logger = logging.getLogger("byconnx.core.request_queue")

class CrawlRequest:
    """Represents a target URL and its traversal metadata."""
    def __init__(self, url: str, depth: int = 0, max_depth: int = 3, payload: Optional[Dict[str, Any]] = None):
        self.url = url
        self.depth = depth
        self.max_depth = max_depth
        self.payload = payload or {}
        self.unique_key = url  # Simple deduplication key
        self.lock_time: Optional[float] = None


class RequestQueue:
    """
    Redis-compatible / In-Memory distributed request queue with item locking.
    Prevents parallel workers from crawling identical pages simultaneously.
    """
    def __init__(self, lock_duration_sec: int = 60):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.seen_urls: Set[str] = set()
        self.in_progress: Dict[str, CrawlRequest] = {}
        self.lock_duration_sec = lock_duration_sec
        self._lock = asyncio.Lock()

    async def add(self, request: CrawlRequest) -> bool:
        """Adds a request to the queue if not already visited/queued."""
        async with self._lock:
            if request.unique_key in self.seen_urls:
                return False
            
            if request.depth > request.max_depth:
                logger.debug(f"Request depth limit reached: {request.url}")
                return False

            self.seen_urls.add(request.unique_key)
            await self.queue.put(request)
            logger.debug(f"Queued target URL: {request.url} (Depth: {request.depth})")
            return True

    async def get_next(self) -> Optional[CrawlRequest]:
        """Retrieves and locks the next request in the queue."""
        async with self._lock:
            # Reclaim expired locked tasks
            now = time.time()
            expired_keys = []
            for key, req in self.in_progress.items():
                if req.lock_time and (now - req.lock_time > self.lock_duration_sec):
                    expired_keys.append(key)
            
            for key in expired_keys:
                req = self.in_progress.pop(key)
                req.lock_time = None
                await self.queue.put(req)
                logger.warning(f"Re-queued stalled request due to lock expiration: {req.url}")

            if self.queue.empty():
                return None

            request = await self.queue.get()
            request.lock_time = now
            self.in_progress[request.unique_key] = request
            return request

    async def complete(self, request: CrawlRequest):
        """Marks the request as successfully processed, releasing the lock."""
        async with self._lock:
            self.in_progress.pop(request.unique_key, None)
            logger.debug(f"Completed and removed crawl job: {request.url}")

    async def fail(self, request: CrawlRequest, retry_limit: int = 3):
        """Registers a failed request and releases the lock for retrying."""
        async with self._lock:
            self.in_progress.pop(request.unique_key, None)
            retries = request.payload.get("retries", 0)
            if retries < retry_limit:
                request.payload["retries"] = retries + 1
                request.lock_time = None
                # Temporarily remove from seen so we can add it again
                self.seen_urls.discard(request.unique_key)
                await self.queue.put(request)
                logger.info(f"Re-queuing failed page request ({retries + 1}/{retry_limit}): {request.url}")
            else:
                logger.error(f"Failed to crawl request after maximum retries: {request.url}")

    async def is_finished(self) -> bool:
        """Returns True if the queue is empty and no tasks are currently in progress."""
        async with self._lock:
            return self.queue.empty() and len(self.in_progress) == 0
