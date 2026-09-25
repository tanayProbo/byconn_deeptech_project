import argparse
import asyncio
import logging
import json
import os

from byconn.core.crawler import BaseCrawler
from byconn.core.request_queue import RequestQueue, CrawlRequest
from byconn.core.browser_pool import BrowserPool
from byconn.core.session_pool import SessionPool
from byconn.pipeline.cleaning import DataCleaner
from byconn.pipeline.embedder import DocumentEmbedder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("byconnx.cli")

cleaner = DataCleaner()
embedder = DocumentEmbedder(embedding_client=None)
results = []

async def scrape_handler(req: CrawlRequest, page):
    """Callback function for the crawler when a page is loaded."""
    url = page.url
    title = await page.title()
    
    # Wait for body to be loaded
    await page.wait_for_selector("body")
    html_content = await page.content()
    
    # Pipeline: Cleaning
    markdown_content = cleaner.html_to_markdown(html_content)
    
    # Pipeline: Embedding (chunking)
    chunks = embedder.split_into_chunks(markdown_content)
    embeddings = await embedder.generate_dense_embeddings(chunks)
    
    # Save results in memory
    results.append({
        "url": url,
        "title": title,
        "markdown": markdown_content,
        "chunks": len(chunks),
        "embeddings_generated": len(embeddings) > 0
    })
    logger.info(f"Successfully processed {url} -> {len(chunks)} chunks.")

async def run_crawl(url: str, depth: int, concurrency: int, output: str):
    logger.info(f"Starting crawl for {url} with depth {depth} and concurrency {concurrency}")
    
    queue = RequestQueue()
    await queue.add(CrawlRequest(url, max_depth=depth))
    
    browser_pool = BrowserPool(headless=True)
    session_pool = SessionPool()
    
    crawler = BaseCrawler(
        request_queue=queue,
        browser_pool=browser_pool,
        session_pool=session_pool,
        concurrency=concurrency,
        max_memory_percent=98.0
    )
    
    # Run the crawler logic (starts workers)
    await crawler.run(scrape_handler)
    
    # Block until the queue is completely finished
    await crawler.wait_for_completion()
    
    # Write results
    with open(output, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    logger.info(f"Crawl finished. Results saved to {output}")

def cli():
    parser = argparse.ArgumentParser(description="BYCONN-X Data Engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    crawl_parser = subparsers.add_parser("crawl", help="Crawl a website and extract markdown/embeddings")
    crawl_parser.add_argument("url", type=str, help="The target URL to crawl")
    crawl_parser.add_argument("--depth", type=int, default=1, help="Max crawl depth")
    crawl_parser.add_argument("--concurrency", type=int, default=2, help="Number of concurrent browsers")
    crawl_parser.add_argument("--output", type=str, default="byconn_results.json", help="Output JSON file path")
    
    args = parser.parse_args()
    
    if args.command == "crawl":
        asyncio.run(run_crawl(args.url, args.depth, args.concurrency, args.output))

if __name__ == "__main__":
    cli()
