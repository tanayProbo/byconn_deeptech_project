"""
BYCONN-X — unified entry point.

Two modes:
  1. API Server  (default):  python -m byconn.main
     → Starts FastAPI on http://localhost:8000 so the dashboard can call it.

  2. CLI Crawl:               python -m byconn.main crawl <url> [options]
     → Runs a headless crawl and writes JSON results to disk.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

# Load .env file if present (OPENAI_API_KEY, DATABASE_URL, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; env vars can be set manually

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("byconnx.main")

# ---------------------------------------------------------------------------
# FastAPI server — exposes the pipeline to the dashboard UI
# ---------------------------------------------------------------------------

def create_app():
    """Builds and returns the FastAPI application."""
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel

    from byconn.pipeline.cleaning import DataCleaner
    from byconn.pipeline.embedder import DocumentEmbedder
    from byconn.pipeline.entity_extractor import EntityExtractor
    from byconn.core.crawler import ByconnCrawler

    app = FastAPI(
        title="BYCONN-X API",
        description="AI-powered data acquisition engine REST API",
        version="1.0.0"
    )

    # Allow the dashboard HTML to call this API from any origin
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    cleaner = DataCleaner()
    embedder = DocumentEmbedder()
    extractor = EntityExtractor()

    # ---- Request/Response models ----------------------------------------

    class SearchRequest(BaseModel):
        query: str
        mode: str = "fast"   # "fast" | "deep" | "visual"

    class CrawlRequest(BaseModel):
        urls: list[str]
        concurrency: int = 3

    # ---- Endpoints ---------------------------------------------------------

    @app.get("/health")
    def health():
        return {"status": "ok", "engine": "BYCONN-X"}

    @app.post("/api/search")
    async def search(req: SearchRequest):
        """
        Main search endpoint called by the dashboard.
        Crawls the URL (or a search result for the query), cleans the HTML,
        extracts entities with OpenAI, and returns structured insights.
        """
        logger.info(f"Search request: '{req.query}' (mode={req.mode})")

        # Determine target URL from query
        import re
        url_match = re.search(r'https?://\S+', req.query)
        target_url = url_match.group(0) if url_match else f"https://www.google.com/search?q={req.query.replace(' ', '+')}"

        # Crawl the page
        crawler = ByconnCrawler(concurrency=1, stealth_mode=True)
        results = await crawler.run(start_urls=[target_url])

        if not results:
            raise HTTPException(status_code=404, detail="Could not fetch content from the target URL.")

        page = results[0]
        markdown = cleaner.html_to_markdown(page.get("content", ""))

        # Extract entities (requires OPENAI_API_KEY)
        knowledge = await extractor.extract_knowledge(markdown)

        return {
            "url": page["url"],
            "title": page.get("title", ""),
            "summary": knowledge.get("summary", ""),
            "entities": knowledge.get("entities", []),
            "triples": knowledge.get("triples", []),
            "topics": knowledge.get("topics", []),
            "markdown_preview": markdown[:1000]
        }

    @app.post("/api/crawl")
    async def crawl(req: CrawlRequest):
        """Crawl a list of URLs and return extracted text and embeddings."""
        crawler = ByconnCrawler(concurrency=req.concurrency, stealth_mode=True)
        raw_results = await crawler.run(start_urls=req.urls)

        output = []
        for page in raw_results:
            md = cleaner.html_to_markdown(page.get("content", ""))
            chunks = embedder.split_into_chunks(md)
            embeddings = await embedder.generate_dense_embeddings(chunks)
            output.append({
                "url": page["url"],
                "title": page.get("title", ""),
                "chunks": len(chunks),
                "embedding_dim": len(embeddings[0]) if embeddings else 0,
            })
        return {"results": output}

    @app.get("/api/status")
    def status():
        """Returns engine component availability."""
        return {
            "openai_key_set": bool(os.getenv("OPENAI_API_KEY")),
            "postgres_uri_set": bool(os.getenv("DATABASE_URL")),
            "qdrant_host": os.getenv("QDRANT_HOST", "localhost"),
            "neo4j_uri": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        }

    return app


# ---------------------------------------------------------------------------
# CLI crawl mode
# ---------------------------------------------------------------------------

async def run_cli_crawl(url: str, depth: int, concurrency: int, output: str):
    from byconn.core.crawler import ByconnCrawler
    from byconn.pipeline.cleaning import DataCleaner
    from byconn.pipeline.embedder import DocumentEmbedder

    logger.info(f"CLI crawl: {url} | depth={depth} concurrency={concurrency}")
    crawler = ByconnCrawler(concurrency=concurrency, stealth_mode=True)
    results = await crawler.run(start_urls=[url])

    cleaner = DataCleaner()
    embedder = DocumentEmbedder()

    output_data = []
    for page in results:
        md = cleaner.html_to_markdown(page.get("content", ""))
        chunks = embedder.split_into_chunks(md)
        output_data.append({
            "url": page["url"],
            "title": page.get("title", ""),
            "markdown": md,
            "chunks": len(chunks),
        })

    with open(output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    logger.info(f"Results saved → {output}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="BYCONN-X Data Engine")
    subparsers = parser.add_subparsers(dest="command")

    # crawl sub-command
    crawl_p = subparsers.add_parser("crawl", help="Crawl a URL from the CLI")
    crawl_p.add_argument("url")
    crawl_p.add_argument("--depth", type=int, default=1)
    crawl_p.add_argument("--concurrency", type=int, default=2)
    crawl_p.add_argument("--output", default="byconn_results.json")

    # serve sub-command (default)
    subparsers.add_parser("serve", help="Start the FastAPI API server (default)")

    args = parser.parse_args()

    if args.command == "crawl":
        asyncio.run(run_cli_crawl(args.url, args.depth, args.concurrency, args.output))
    else:
        # Default: start the API server
        import uvicorn
        app = create_app()
        logger.info("Starting BYCONN-X API server on http://localhost:8000")
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
