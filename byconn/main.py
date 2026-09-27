"""BYCONN-X command line interface.

Crawls a site with real frontier expansion, so ``--depth`` controls how far
link discovery follows from the seed URL.
"""

import argparse
import asyncio
import logging
import json
import os
from typing import List, Optional

from byconn.core.crawler import BaseCrawler
from byconn.core.request_queue import RequestQueue, CrawlRequest
from byconn.core.browser_pool import BrowserPool
from byconn.core.link_discovery import extract_links, load_robots, is_allowed
from byconn.core.session_pool import SessionPool
from byconn.pipeline.cleaning import DataCleaner
from byconn.pipeline.embedder import DocumentEmbedder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("byconnx.cli")

# Frontier limits, overridable from the environment.
MAX_PAGES = max(1, int(os.getenv("CRAWL_MAX_PAGES", "50")))
FOLLOW_LINKS = os.getenv("CRAWL_FOLLOW_LINKS", "true").lower() not in {"0", "false", "no"}
DEDUPLICATE = os.getenv("CRAWL_DEDUPLICATE", "true").lower() not in {"0", "false", "no"}


async def scrape_handler(req: CrawlRequest, page, state: dict):
    """Processes one crawled page and expands the frontier.

    Args:
        req: The dequeued request.
        page: Live Playwright page.
        state: Mutable crawl state (cleaner, embedder, results, host, robots,
            queue, job counters).
    """
    url = page.url or req.url

    # Hard page cap. An enqueue-time check alone is not enough: with N workers
    # several links can already be queued when the cap is reached. Checked
    # before the counter moves so the cap is a true upper bound.
    if state["pages"] >= MAX_PAGES:
        logger.info("Page cap %d reached; skipping %s", MAX_PAGES, url)
        return

    # Wait for body to be loaded
    try:
        await page.wait_for_selector("body", timeout=10000)
    except Exception as exc:
        logger.debug("body selector wait failed for %s: %s", url, exc)

    title = ""
    html_content = ""
    try:
        title = await page.title()
        html_content = await page.content()
    except Exception as exc:
        logger.error("Failed to read page %s: %s", url, exc)
        return

    state["pages"] += 1

    # Pipeline: Cleaning (with near-duplicate suppression)
    if DEDUPLICATE:
        markdown_content = state["cleaner"].html_to_unique_markdown(html_content)
        if markdown_content is None:
            logger.info("Duplicate of an earlier page, skipping %s", url)
            state["duplicates"] += 1
            return
    else:
        markdown_content = state["cleaner"].html_to_markdown(html_content)

    # Pipeline: Embedding (chunking)
    chunks = state["embedder"].split_into_chunks(markdown_content)
    embeddings = await state["embedder"].generate_dense_embeddings(chunks)

    state["results"].append({
        "url": url,
        "title": title,
        "markdown": markdown_content,
        "chunks": len(chunks),
        "embeddings_generated": len(embeddings) > 0,
        "depth": req.depth
    })
    logger.info(f"Successfully processed {url} -> {len(chunks)} chunks (depth {req.depth}).")

    # Frontier expansion: follow same-host links up to the requested depth.
    if not FOLLOW_LINKS or req.depth >= req.max_depth:
        if req.depth >= req.max_depth and req.max_depth > 0:
            logger.info("Depth limit %d reached at %s", req.max_depth, url)
        return
    if state["pages"] >= MAX_PAGES:
        logger.info("Page cap %d reached; not expanding %s", MAX_PAGES, url)
        return

    for link in extract_links(html_content, url, state["host"]):
        if state["pages"] >= MAX_PAGES:
            logger.info("Page cap %d reached; not enqueueing %s", MAX_PAGES, link)
            break
        if not is_allowed(state["robots"], link):
            logger.debug("robots.txt disallows %s", link)
            continue
        await state["queue"].add(
            CrawlRequest(link, depth=req.depth + 1, max_depth=req.max_depth)
        )


async def run_crawl(url: str, depth: int, concurrency: int, output: str):
    logger.info(f"Starting crawl for {url} with depth {depth} and concurrency {concurrency}")

    queue = RequestQueue()
    await queue.add(CrawlRequest(url, depth=0, max_depth=depth))

    from urllib.parse import urlparse
    host = urlparse(url).netloc
    robots = await load_robots(url) if FOLLOW_LINKS else None

    state = {
        "cleaner": DataCleaner(),
        "embedder": DocumentEmbedder(embedding_client=None),
        "results": [],
        "host": host,
        "robots": robots,
        "queue": queue,
        "pages": 0,
        "duplicates": 0,
    }

    crawler = BaseCrawler(
        request_queue=queue,
        browser_pool=BrowserPool(headless=True),
        session_pool=SessionPool(),
        concurrency=concurrency,
        max_memory_percent=98.0
    )

    async def handler(request, page):
        await scrape_handler(request, page, state)

    # Run the crawler logic (starts workers)
    await crawler.run(handler)

    # Block until the queue is completely finished
    await crawler.wait_for_completion()

    payload = {
        "seed": url,
        "max_depth": depth,
        "pages_crawled": state["pages"],
        "duplicates_skipped": state["duplicates"],
        "results": state["results"],
    }

    # Write results
    with open(output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    logger.info(
        "Crawl finished. %d pages (%d duplicates skipped). Results saved to %s",
        state["pages"], state["duplicates"], output,
    )


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI parser.

    Two equivalent invocations are supported:

        python -m byconn.main --url https://example.com --depth 1
        python -m byconn.main crawl https://example.com --depth 1
        byconn crawl https://example.com          # console script

    Subcommand options default to ``SUPPRESS`` so that omitting them there does
    not clobber a value given before the subcommand.
    """
    parser = argparse.ArgumentParser(
        prog="byconn",
        description="BYCONN-X Data Engine CLI",
    )
    parser.add_argument("--url", type=str, default=None,
                        help="The target URL to crawl")
    parser.add_argument("--depth", type=int, default=1,
                        help="Max link-following depth (0 = seed URL only)")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="Number of concurrent browsers")
    parser.add_argument("--output", type=str, default="byconn_results.json",
                        help="Output JSON file path")

    subparsers = parser.add_subparsers(dest="command", metavar="{crawl}")

    crawl = subparsers.add_parser(
        "crawl",
        help="Crawl a website and extract markdown/embeddings",
        description="Crawl a website and extract markdown/embeddings.",
    )
    crawl.add_argument("url", type=str, nargs="?", default=argparse.SUPPRESS,
                       help="The target URL to crawl")
    crawl.add_argument("--depth", type=int, default=argparse.SUPPRESS,
                       help="Max link-following depth (0 = seed URL only)")
    crawl.add_argument("--concurrency", type=int, default=argparse.SUPPRESS,
                       help="Number of concurrent browsers")
    crawl.add_argument("--output", type=str, default=argparse.SUPPRESS,
                       help="Output JSON file path")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    """Entry point for the console script and ``python -m byconn.main``."""
    parser = build_parser()
    args = parser.parse_args(argv)

    url = getattr(args, "url", None)
    if not url:
        parser.error(
            "a target URL is required: use --url <URL> or 'crawl <URL>'"
        )

    asyncio.run(run_crawl(url, max(0, args.depth), args.concurrency, args.output))


# Backwards-compatible alias used by the console script and __main__.
def cli() -> None:
    """Alias for :func:`main`."""
    main()


if __name__ == "__main__":
    main()
