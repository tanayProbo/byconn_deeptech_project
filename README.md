# BYCONN-X: Universal AI-Powered Data Acquisition

**BYCONN-X** is an AI-native data acquisition engine for crawling, scraping and
understanding the web at scale. It combines a distributed Playwright crawler
with real LLM-powered entity extraction and a knowledge-graph store.

## Features

- **Distributed Crawling** — concurrent Playwright workers with fingerprint
  spoofing, proxy rotation, memory-aware throttling and item-locked retries.
- **AI Extraction** — `LLMExtractor` turns cleaned page text into typed
  entities and relation triples via OpenAI or Gemini, with tiktoken budgeting
  and retry/backoff.
- **Semantic Vectors** — real 384-dim embeddings (OpenAI
  `text-embedding-3-small` or local `sentence-transformers`) indexed in Qdrant.
- **Knowledge Graph** — entities and relations merged into Neo4j with
  allowlisted labels and fully parameterised Cypher.
- **Structured Storage** — raw pages and extracted entities persisted in
  PostgreSQL with schema auto-creation.
- **REST API** — FastAPI service with a background crawl pipeline, job tracking
  and a health endpoint.
- **Operator Dashboard** — a neobrutalist search console served by the API.

## Quickstart & Demo

Four commands take you from a clean clone to a live crawl and dashboard.

### 1. Install

```bash
git clone https://github.com/tanayProbo/byconn_deeptech_project.git
cd byconn_deeptech_project

python -m venv venv && source venv/bin/activate   # Windows: .\venv\Scripts\activate
pip install -e .                                  # installs deps + byconn / byconn-server
playwright install chromium                        # downloads the browser binary
```

`pip install -e .` is what provides the `byconn` and `byconn-server` commands
used below. If you only run `pip install -r requirements.txt` you get the
libraries but neither entry point.

### 2. Configure `.env`

Every value is optional — the service starts with none of them and reports
`degraded` health. Copy the template and add whichever keys you have:

```bash
cp .env.example .env
```

**Option A — free and offline (Ollama, no API key, nothing leaves your machine).**

```bash
ollama pull llama3.2     # text extraction
ollama pull llava        # screenshots, for the visual agent
ollama serve
```

```bash
# .env
OPENAI_API_KEY=ollama                     # implies http://localhost:11434/v1
MODEL_NAME=llama3.2
VISION_MODEL_NAME=llava
```

`OPENAI_API_KEY=ollama` is the switch that selects the local endpoint, so
`OPENAI_API_BASE` can be omitted. Any OpenAI-compatible server (vLLM, LM Studio,
llama.cpp, TGI) works the same way — set `OPENAI_API_BASE` to its `/v1` URL.

**Option B — free hosted tier (Groq):**

```bash
# .env
GROQ_API_KEY=gsk_...
MODEL_NAME=llama-3.3-70b-versatile
```

**Option C — paid hosted (OpenAI or Gemini):**

```bash
# .env — the provider is auto-detected; OpenAI wins if both are present.
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

Vector search is separate and needs an embedding provider. With neither of the
following the crawl still stores pages but skips vector indexing:

```bash
pip install -e ".[local-embeddings]"   # local model, no API key needed
# OPENAI_API_KEY=sk-...                # or reuse a real hosted OpenAI key
```

Full reference: [Configuration](#configuration).

### 3. Start the backend

```bash
byconn-server                       # honours HOST/PORT (default 0.0.0.0:8000)
# or, with autoreload for development:
uvicorn server:app --reload
```

Open <http://localhost:8000/docs> for the interactive API reference.

### 4. Run a crawl

Either through the API:

```bash
curl -X POST http://localhost:8000/api/v1/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://books.toscrape.com/", "max_depth": 1}'

# -> {"job_id":"6f2a...","status":"queued",...}

curl http://localhost:8000/api/v1/crawl/<job_id>
```

Or from the CLI:

```bash
python -m byconn.main --url https://books.toscrape.com/ --depth 1
```

The CLI writes `byconn_results.json`:

```json
{
  "seed": "https://books.toscrape.com/",
  "max_depth": 1,
  "pages_crawled": 50,
  "duplicates_skipped": 1,
  "results": [
    { "url": "https://books.toscrape.com/", "title": "All books",
      "markdown": "# All books 1000 books  ...",
      "chunks": 3, "embeddings_generated": true, "depth": 0 }
  ]
}
```

`crawl <URL>` also works and is what the `byconn` console script uses:

```bash
byconn crawl https://books.toscrape.com/ --depth 1 --concurrency 2
```

### 5. View the dashboard

<http://localhost:8000/dashboard>

The operator console is served by the same process — enter a target URL, watch
the agent terminal stream progress, and inspect engine health on the **API
Keys** tab. There is nothing extra to run.

### Recording a demo

The two-terminal sequence for a screen recording:

```bash
# Terminal 1 — the model server and configuration
ollama serve
cat .env                # OPENAI_API_KEY=ollama / MODEL_NAME / VISION_MODEL_NAME
```

```bash
# Terminal 2 — the service and dashboard
byconn-server
```

Then browse to <http://localhost:8000/dashboard>, submit a URL, and record the
crawl. Watch <http://localhost:8000/health> (or `/api/v1/health`) in a second tab
to show which dependencies are live.

Counters on a finished job only ever report work that was actually persisted:
`entities_extracted` counts what the model returned, while `pages_saved`,
`chunks_indexed` and `relations_written` count what the stores accepted. If a
datastore is down the crawl still succeeds and the shortfall appears in the
job's `errors` array.

### What just happened

For every page the pipeline crawls, cleans to Markdown, deduplicates against
earlier pages, embeds and indexes in Qdrant, extracts typed entities and
relations with the LLM, and merges them into Neo4j — while the sniffer records
the site's own XHR traffic and writes an OpenAPI spec to `byconn_output/`.

### Troubleshooting

| Symptom | Cause |
| --- | --- |
| `/api/v1/health` returns `503 degraded` | One or more of Postgres/Qdrant/Neo4j is unreachable. Each component is listed with its own status. |
| `"embeddings": "disabled"` | No embedding provider. Run `pip install -e ".[local-embeddings]"` or set a real hosted `OPENAI_API_KEY`. |
| `"llm": "disabled"` | No LLM endpoint configured. Set `OPENAI_API_KEY=ollama` for a local model, `GROQ_API_KEY` for the free tier, or `OPENAI_API_KEY` / `GEMINI_API_KEY`. Pages are still crawled and stored. |
| `Executable doesn't exist ... chromium` | Run `playwright install chromium`. |

---

## Requirements

- Python 3.10+
- Optionally a PostgreSQL, Qdrant and Neo4j instance (all connection details
  come from the environment and fall back to localhost defaults)

## Setup

```bash
git clone https://github.com/tanayProbo/byconn_deeptech_project.git
cd byconn_deeptech_project

python -m venv venv
source venv/bin/activate          # Windows: .\venv\Scripts\activate

pip install -e .
playwright install chromium        # downloads the browser binary
```

For key-free local embeddings, install the optional extra:

```bash
pip install -e ".[local-embeddings]"   # pulls in sentence-transformers
```

## Configuration

Configuration is read from the environment; a `.env` file in the project
root is loaded automatically (`cp .env.example .env` to start). Every
value is optional and falls back to a local default, so the service
boots with no configuration at all.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/byconnx` | PostgreSQL DSN |
| `POSTGRES_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_DB` | `localhost` / `5432` / `postgres` / `postgres` / `byconnx` | Used when `DATABASE_URL` is unset |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant endpoint |
| `QDRANT_HOST` / `QDRANT_PORT` / `QDRANT_HTTPS` | `localhost` / `6333` / `false` | Used when `QDRANT_URL` is unset |
| `QDRANT_API_KEY` | *(unset)* | Qdrant auth |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j bolt URI |
| `NEO4J_USER` / `NEO4J_PASSWORD` | `neo4j` / `password` | Neo4j credentials |
| `NEO4J_DATABASE` | *(server default)* | Neo4j database name |
| `LLM_PROVIDER` | `auto` | `openai`, `groq`, `gemini` or `auto` |
| `OPENAI_API_KEY` | *(unset)* | OpenAI-compatible credentials. The literal `ollama` selects the local endpoint |
| `OPENAI_API_BASE` | *(provider default)* | Base URL for any OpenAI-compatible server. Implied as `http://localhost:11434/v1` when `OPENAI_API_KEY=ollama` |
| `GROQ_API_KEY` | *(unset)* | Groq free-tier key; base URL and model default automatically |
| `MODEL_NAME` | `llama3.2` local / `gpt-4o-mini` hosted | Extraction model for any OpenAI-compatible endpoint |
| `VISION_MODEL_NAME` | `llava` local / `gpt-4o-mini` hosted | Model used for screenshot-driven agent decisions |
| `OPENAI_MODEL` / `GEMINI_MODEL` | *(unset)* | Legacy per-provider model overrides, used when `MODEL_NAME` is unset |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | *(unset)* / `gemini-3.8-flash` | Gemini credentials and model |
| `HEALTH_PROBE_TIMEOUT` | `2.0` | Seconds before a dependency is reported down by the health endpoint |
| `EMBEDDING_PROVIDER` | `auto` | `sentence-transformers`, `openai` or `auto` |
| `EMBEDDING_MODEL` | provider default | Embedding model id |
| `EMBEDDING_DIMENSIONS` | `384` | Must match your Qdrant collection |
| `CRAWL_CONCURRENCY` | `2` | Simultaneous crawls |
| `CRAWL_MAX_PAGES` | `50` | Hard page cap per crawl |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | API bind address |

Without an embedding provider the pipeline still crawls, cleans, extracts and
stores pages; it simply skips vector indexing and reports
`"embeddings": "disabled"` from `/api/v1/health`.

## Running the API

```bash
uvicorn server:app --reload
```

Then open:

- API docs — <http://localhost:8000/docs>
- Dashboard — <http://localhost:8000/dashboard/>
- Health — <http://localhost:8000/api/v1/health>

A console script is also installed: `byconn-server` (host/port via `HOST` and
`PORT`).

### Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/v1/crawl` | Queue a crawl. Body: `{"url": str, "max_depth": int}`. Returns `202` with a `job_id`. |
| `GET` | `/api/v1/crawl/{job_id}` | Job status and counters. |
| `GET` | `/api/v1/health` | Per-dependency status. `200` healthy, `503` degraded. |
| `GET` | `/dashboard/` | Static operator console. |

## Library usage

Crawl a site and run the full pipeline directly:

```python
import asyncio
from byconn.storage.adapters import PostgresAdapter, QdrantAdapter, Neo4jAdapter
from byconn.pipeline.llm_extractor import LLMExtractor
from byconn.core.crawler import BaseCrawler
from byconn.core.request_queue import CrawlRequest, RequestQueue
from byconn.core.browser_pool import BrowserPool
from byconn.core.session_pool import SessionPool
from byconn.pipeline.cleaning import DataCleaner

async def main():
    cleaner = DataCleaner()
    extractor = LLMExtractor()

    async with PostgresAdapter() as pg, QdrantAdapter() as qdrant, Neo4jAdapter() as neo4j:
        queue = RequestQueue()
        await queue.add(CrawlRequest("https://example.com", max_depth=1))

        async def handler(request, page):
            html = await page.content()
            cleaned = cleaner.clean_html(html)
            page_id = await pg.upsert_crawled_page(request.url, markdown=html)

            knowledge = await extractor.extract_knowledge(cleaned)
            await pg.insert_entities(page_id, knowledge["entities"], source_url=request.url)
            await neo4j.upsert_page(request.url)
            await neo4j.write_triples(knowledge["triples"],
                                       properties={"source_url": request.url})

        crawler = BaseCrawler(
            request_queue=queue,
            browser_pool=BrowserPool(headless=True),
            session_pool=SessionPool(),
            concurrency=3,
        )
        await crawler.run(handler)
        await crawler.wait_for_completion()

asyncio.run(main())
```

Extract structured knowledge on its own:

```python
import asyncio
from byconn.pipeline.llm_extractor import LLMExtractor

result = asyncio.run(LLMExtractor().extract_knowledge(page_html))
# {"entities": [{"name": ..., "type": ...}], "triples": [...], "topics": [...], "summary": ...}
```

Clean and chunk a document:

```python
from byconn.pipeline.cleaning import DataCleaner
from byconn.pipeline.embedder import DocumentEmbedder

cleaner = DataCleaner()
markdown = cleaner.html_to_markdown(raw_html)          # tables and lists preserved

embedder = DocumentEmbedder()                          # auto-selects a provider
chunks = embedder.split_into_chunks(markdown)
vectors = asyncio.run(embedder.generate_dense_embeddings(chunks))
```

## Command line

The original single-page crawler is still available and now expands the
frontier, so `--depth` genuinely controls how far link discovery follows:

```bash
# Documented form
python -m byconn.main --url https://example.com --depth 1

# Equivalent subcommand form, used by the `byconn` console script
byconn crawl https://example.com --depth 1 --concurrency 2 --output results.json
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--url` / positional `url` | *required* | Target URL. Must be `http(s)`. |
| `--depth` | `1` | Link-following depth. `0` crawls only the seed URL. |
| `--concurrency` | `2` | Concurrent browser workers. |
| `--output` | `byconn_results.json` | Output JSON path. |

Discovery stays on the seed host, honours `robots.txt`, and stops at
`CRAWL_MAX_PAGES` (default 50). Set `CRAWL_DEDUPLICATE=false` to disable
near-duplicate suppression, or `CRAWL_FOLLOW_LINKS=false` for a single page.

## Tests

```bash
pytest
```

The suite is hermetic: PostgreSQL, Qdrant, Neo4j, the browser and the LLM are
all replaced with test doubles, and no network access or running services are
required.

## Project layout

```
server.py                        FastAPI application and background pipeline
conftest.py                      pytest sys.path bootstrap
byconn/
  core/                          crawler, request queue, browser/session pools
  pipeline/                      cleaning, chunking, embedding, LLM extraction
  storage/adapters.py            async PostgreSQL, Qdrant and Neo4j adapters
  api_intelligence/              network interception and OpenAPI generation
  free_api_integration/          public API registry and connector generation
  mobile/                        ADB and scrcpy device automation
  visual_agent/                  DOM-driven browser agent
  dashboard/                     static operator console
  deployment/                    Helm and Terraform configuration
  docs/blueprint.md              system design document
  tests/                         pytest suite
```

## License

MIT License — Copyright © 2026 Tanay Praveen Agrawal
