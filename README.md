# BYCONN-X: schema-shaped web data, with a source for every value

[![CI](https://github.com/tanayProbo/byconn_deeptech_project/actions/workflows/ci.yml/badge.svg)](https://github.com/tanayProbo/byconn_deeptech_project/actions/workflows/ci.yml)

**BYCONN-X** crawls a site with Playwright and returns exactly the data you ask
for, in the JSON Schema you give it. Every extracted value carries a quote from
the page, and **the quote is checked against the page in code**. A value the
model cannot back up is flagged as *unverified* rather than passed off as fact.
Along the way it stores pages in PostgreSQL, embeds them into Qdrant, builds a
knowledge graph in Neo4j, and records the site's own API calls as an OpenAPI
spec.

The shape of a result (`GET /api/v1/crawl/{id}/results`, abridged):

```text
structured.data.products[0]            {"name": "A Light in the ...", "price": "£51.77"}
structured.citations["/products/0/price"]  [{"quote": "£51.77", "url": "https://books.toscrape.com/..."}]
structured.unverified                  ["/products/7/name"]   ← no quote on the page supports this value
```

## What is built, and what is not

This table is kept honest on purpose: every "Implemented" row links to the code
that does it, and every claim can be checked with `pytest`.

| Capability | Status | Where |
| --- | --- | --- |
| Playwright crawling: concurrent workers, memory-aware throttling, retries, bounded navigation and per-page time limits | Implemented | [`core/crawler.py`](byconn/core/crawler.py), [`core/request_queue.py`](byconn/core/request_queue.py) |
| Same-host frontier, `robots.txt` for the seed and every discovered link, page cap | Implemented | [`core/link_discovery.py`](byconn/core/link_discovery.py), [`server.py`](server.py) |
| HTML → Markdown cleaning with per-job near-duplicate suppression | Implemented | [`pipeline/cleaning.py`](byconn/pipeline/cleaning.py) |
| **Schema-driven extraction with verified citations**, retry on schema errors, windowing for long pages | Implemented | [`pipeline/structured.py`](byconn/pipeline/structured.py), [`pipeline/llm_extractor.py`](byconn/pipeline/llm_extractor.py) |
| Entity and relation extraction (any OpenAI-compatible model, Groq, Gemini, local Ollama), content-hash cache, concurrency cap | Implemented | [`pipeline/llm_extractor.py`](byconn/pipeline/llm_extractor.py) |
| Embeddings (local sentence-transformers or OpenAI) indexed in Qdrant | Implemented | [`pipeline/embedder.py`](byconn/pipeline/embedder.py), [`storage/adapters.py`](byconn/storage/adapters.py) |
| PostgreSQL pages, entities and per-page results; Neo4j knowledge graph | Implemented | [`storage/adapters.py`](byconn/storage/adapters.py) |
| API sniffing of XHR/fetch traffic → OpenAPI spec | Implemented | [`api_intelligence/`](byconn/api_intelligence) |
| REST API with live progress over server-sent events, results, history and JSON/JSONL/CSV export | Implemented | [`server.py`](server.py) |
| Operator console: live job log, cited data tables, entities, graph, exports | Implemented | [`dashboard/`](byconn/dashboard) |
| Visual browser agent (plans over the page's interactive elements, plus a screenshot for vision models) | Implemented | [`visual_agent/`](byconn/visual_agent) |
| Extraction eval: 20 fixtures with gold answers, field-level P/R/F1 | Implemented | [`eval/`](byconn/eval) |
| Browser fingerprinting | Partial: rotated user agents and `navigator.webdriver` hidden | [`core/browser_pool.py`](byconn/core/browser_pool.py) |
| Proxy rotation and session scoring | Partial: `BrowserPool` accepts a proxy list and `SessionPool` exists, but the server wires neither in | [`core/browser_pool.py`](byconn/core/browser_pool.py), [`core/session_pool.py`](byconn/core/session_pool.py) |
| Android automation | Partial: ADB commands and the accessibility inspector work; the scrcpy frame decoder is a stub; not exposed by the API | [`mobile/`](byconn/mobile) |
| Public API catalogue (18 APIs), connector generator, health monitor | Library only; not exposed by the API | [`free_api_integration/`](byconn/free_api_integration) |
| Terraform (GKE) | Partial: a skeleton, not exercised in CI | [`deployment/terraform/`](byconn/deployment/terraform) |
| Helm chart | Planned: `Chart.yaml` and `values.yaml` only, no templates yet | [`deployment/helm/`](byconn/deployment/helm) |
| Distributed crawling across machines, per-domain rate limiting, PII redaction, Kafka, Ray, ClickHouse, Vault | Planned; not in the code | [`docs/blueprint.md`](byconn/docs/blueprint.md) describes the target design |

## Architecture

```mermaid
flowchart LR
    API["POST /api/v1/crawl<br/>url · prompt · schema"] --> Q["RequestQueue<br/>same host · robots.txt · page cap"]
    Q --> W["Playwright workers<br/>BrowserPool"]
    W -. "XHR / fetch" .-> SN["ProxySniffer → OpenAPI spec"]
    W --> C["DataCleaner<br/>HTML → Markdown · per-job dedup"]
    C --> PG1["PostgreSQL<br/>raw page"]
    C --> V["Embedder → Qdrant"]
    C --> K["LLM: entities + relations"]
    C --> SX["LLM: schema data + quotes<br/>quotes verified against the page"]
    K --> PG2["PostgreSQL<br/>entities"]
    K --> N["Neo4j graph"]
    SX --> R["Per-page results<br/>merged into the job"]
    K --> R
    R --> PG3["PostgreSQL<br/>extraction_results"]
    R --> SSE["SSE /events"] --> UI["Dashboard"]
```

**Design decisions**

- **Quotes are verified, not trusted.** A model asked for citations will
  happily invent them. [`verify_citations`](byconn/pipeline/structured.py)
  keeps a quote only if it occurs in the page text after normalising case,
  whitespace, typography and Markdown markup. Everything else is reported in
  `unverified` and flagged in the UI and exports.
- **Independent steps run concurrently.** Per page, the PostgreSQL save,
  embedding + Qdrant indexing, knowledge extraction and schema extraction run
  together, so a page costs its slowest step, not the sum. The graph and
  entity writes form a second concurrent phase.
- **Content-hash LLM cache.** Identical text sent to the same model gives the
  same answer, so it is not sent twice (`LLM_CACHE_SIZE`). Failures are never
  cached. Provider calls are capped (`LLM_MAX_CONCURRENCY`) to stay inside
  free-tier rate limits.
- **Degrade, never lie.** Any store can be down: the crawl still finishes,
  results stay available from memory, the shortfall is listed in the job's
  `errors`, and `/api/v1/health` answers `503 degraded`. Counters only count
  what a store accepted. Unreachable stores fail fast instead of stalling each
  page.

## Quickstart & Demo

A clean clone to a live crawl and dashboard, with every store green.

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

### 2. Start the datastores

```bash
docker compose up -d
```

That starts PostgreSQL, Qdrant and Neo4j with the schema and collection already
created. The service **starts without them** and degrades gracefully, but then
`/api/v1/health` reports each store as `down`, vector indexing is skipped, and a
crawl stores nothing — so do this before recording a demo. Check it with:

```bash
curl -s localhost:8000/api/v1/health | python -m json.tool
# "status": "ok"  <- every component up
```

Shut down with `docker compose down` (add `-v` to delete the data).

### 3. Configure `.env`

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

### 4. Start the backend

```bash
byconn-server                       # honours HOST/PORT (default 0.0.0.0:8000)
# or, with autoreload for development:
uvicorn server:app --reload
```

Open <http://localhost:8000/docs> for the interactive API reference.

### 5. Run a crawl

Either through the API:

```bash
curl -X POST http://localhost:8000/api/v1/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://books.toscrape.com/", "max_depth": 1,
       "prompt": "extract every book title and price",
       "schema": {"type": "object", "properties": {"products": {"type": "array",
                  "items": {"type": "object", "properties": {
                     "name": {"type": "string"}, "price": {"type": "string"}}}}}}}'

# -> {"job_id":"6f2a...","status":"queued","has_schema":true,...}

curl -N http://localhost:8000/api/v1/crawl/<job_id>/events    # live progress (SSE)
curl http://localhost:8000/api/v1/crawl/<job_id>/results       # data, citations, graph
curl -OJ "http://localhost:8000/api/v1/crawl/<job_id>/export?format=csv"
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

### 6. View the dashboard

<http://localhost:8000/dashboard>

The operator console is served by the same process. Type a URL with an
instruction, optionally pick an output schema preset, and launch. The job card
streams progress live; the results show the data table (each value has a
**source** marker that opens its verified quote), pages, entities, the graph
and download links. **Job History** survives a page refresh. There is nothing
extra to run.

### Recording a demo

`scripts/demo_up.sh` starts the datastores and the server and waits until
every dependency reports up. The 90-second shot list is in
[`docs/demo.md`](byconn/docs/demo.md).

The search box accepts a bare domain, a full URL, or an instruction with a URL
in it (`extract pricing from stripe.com`); the prose becomes the extraction
instruction (or the agent's task in **Visual Action** mode).

**Keeping it fast without faking it.** Every step really runs; speed comes
from doing less redundant work:

- Per page, the PostgreSQL save, embedding + Qdrant indexing and LLM
  extraction run concurrently, so a page costs its slowest step, not the sum.
- LLM results are cached by content hash (`LLM_CACHE_SIZE`, default 256), so
  a rehearsal re-crawl of the demo site skips repeat model calls. The cache is
  in memory: restart the server for a cold run.
- For a hosted model, a small fast one keeps latency low on Groq's free tier:

  ```bash
  GROQ_API_KEY=...                     # from console.groq.com, never committed
  MODEL_NAME=llama-3.1-8b-instant
  CRAWL_MAX_PAGES=10                   # a short, honest crawl for a live demo
  ```

- Start the datastores before the server: with them down every write waits
  out its timeout and `/api/v1/health` reports the service as degraded.

Counters on a finished job only ever report work that was actually persisted:
`entities_extracted` counts what the model returned, while `pages_saved`,
`chunks_indexed` and `relations_written` count what the stores accepted. If a
datastore is down the crawl still succeeds and the shortfall appears in the
job's `errors` array.

### What just happened

For every page the pipeline crawls, cleans to Markdown, deduplicates against
earlier pages of the same job, embeds and indexes in Qdrant, extracts typed
entities and relations (and, when asked, schema-shaped data with verified
quotes) with the LLM, and merges the relations into Neo4j — while the sniffer records
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
| `CHUNK_SIZE` | `180` | Words per embedding chunk; keep under ~190 for MiniLM's 256-token window |
| `PAGE_HANDLER_TIMEOUT` | `180` | Seconds before one page's processing is abandoned (not retried) |
| `LLM_CACHE_SIZE` | `256` | Extraction results cached by content hash; `0` disables |
| `LLM_MAX_CONCURRENCY` | `4` | Parallel provider calls; lower it on strict free tiers |
| `LLM_MAX_WINDOWS` | `4` | Windows of a long page sent for schema extraction |
| `LLM_MAX_INPUT_TOKENS` | `6000` | Tokens of page text per model call (per window) |
| `NEO4J_MAX_RETRY_TIME` | `5` | Seconds the Neo4j driver retries a failed transaction |
| `SSE_HEARTBEAT_SECONDS` | `15` | Heartbeat interval on idle event streams |
| `GRAPH_NODE_CAP` | `300` | Largest knowledge graph returned to the dashboard |
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
| `POST` | `/api/v1/crawl` | Queue a crawl. Body: `url`, `max_depth` (0–5), optional `prompt` and `schema` (JSON Schema, `"type": "object"`). Returns `202` with a `job_id`. |
| `GET` | `/api/v1/crawl/{job_id}` | Job status and counters. |
| `GET` | `/api/v1/crawl/{job_id}/events` | Live progress as server-sent events: `snapshot`, `status`, `page`, `job_error`, `done`. |
| `GET` | `/api/v1/crawl/{job_id}/results` | Per-page results, merged structured data with citations and unverified pointers, and the knowledge graph. |
| `GET` | `/api/v1/crawl/{job_id}/export?format=json\|jsonl\|csv` | Download. CSV flattens the structured array of objects and adds a `sources` column. |
| `POST` | `/api/v1/act` | Queue a visual-agent task: `url`, `task`, `max_steps`. |
| `GET` | `/api/v1/act/{job_id}` and `/events` | Agent status with its step trace; live steps over SSE. |
| `GET` | `/api/v1/jobs` | Recent crawl and agent jobs, newest first. |
| `GET` | `/api/v1/apis` | Endpoints discovered by the API sniffer (from PostgreSQL). |
| `GET` | `/api/v1/health` | Per-dependency status plus the LLM provider and model. `200` healthy, `503` degraded. |
| `GET` | `/dashboard/` | Operator console. |

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

## Evaluation

`python -m byconn.eval` runs the real cleaning and structured-extraction path
over 20 fixtures and scores the output against gold answers:

- 6 book catalogue pages and 6 quotation pages from the Zyte scraping sandboxes
  (books.toscrape.com, quotes.toscrape.com);
- 8 hand-written pages with traps: a struck-through old price, filled jobs, past
  events, a customer quote on a team page, a "was" price column.

Gold contains only what is visible in the text the model receives, and a test
enforces that for every value. Scoring is field-level: a value is right when it
matches after normalisation (numbers must agree numerically), an invented
record costs precision, and a missed one costs recall. Details:
[`eval/scoring.py`](byconn/eval/scoring.py) and
[`eval/fixtures/README.md`](byconn/eval/fixtures/README.md).

```bash
GROQ_API_KEY=... python -m byconn.eval --model llama-3.1-8b-instant
OPENAI_API_KEY=ollama python -m byconn.eval --model llama3.2     # local
```

Each run writes `byconn/eval/results/<date>-<model>.json` and `.md`. With no
model configured the command exits without writing anything, so every number
below comes from a real run.

### Results

| Model | Where | Field P | Field R | Field F1 | Values with a verified quote | Schema-valid | Latency p50 / p95 | Run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `llava` 7B | local (Ollama), 1800-token windows | 75.5% | 63.4% | 69.0% | 88.7% | 80% | 21s / 58s | [2026-09-27](byconn/eval/results/2026-09-27-llava.md) |

This is a deliberately modest baseline: a small, general vision model on a
laptop, with no tuning. It gives useful lessons:

- **The citation check catches invention.** On `books-history` the model
  invented 16 records, and 16 values came back flagged unverified. Across all
  fixtures, 35 invented records cost precision, and the
  UI and exports mark their values instead of presenting them as facts.
- **Format failures dominate the misses.** Three fixtures got no parseable JSON
  at all (F1 0), so the gap to a stronger model is mostly reliability, not
  reading ability. The 17 fixtures it did parse averaged 0.83 F1.
- **Traps work.** The struck-through price, "was" column and past-events
  fixtures (`synthetic-pricing`, `synthetic-laptops`, `synthetic-events`) are
  where a careless reader loses points.

Add a row by running the eval with another model. Numbers are only ever copied
from a results file.


## Responsible use

What is enforced today: `robots.txt` for the seed URL and every discovered
link, a same-host frontier, a hard page cap per crawl, bounded navigation and
processing time, and a descriptive user agent for `robots.txt` checks.

What is **not** enforced yet: per-domain rate limiting (concurrency is capped,
but requests are not spaced per host) and PII redaction. Crawl only sites you
are allowed to, and keep `CRAWL_PAGE_CONCURRENCY` low for small sites.

## Security

Keys are read from the environment only and never reach the dashboard. CI scans
every push for committed secrets. See [SECURITY.md](SECURITY.md), including a
known key exposure in this repository's history that must be revoked at the
provider.

## Tests

```bash
pytest
```

The suite is hermetic: PostgreSQL, Qdrant, Neo4j, the browser and the LLM are
all replaced with test doubles, and no network access or running services are
required. CI runs it on Python 3.10, 3.11 and 3.12.

## Project layout

```
server.py                        FastAPI application and background pipeline
conftest.py                      pytest sys.path bootstrap
byconn/
  core/                          crawler, request queue, browser/session pools
  pipeline/                      cleaning, chunking, embedding, LLM extraction,
                                 schema extraction with citation checks
  storage/adapters.py            async PostgreSQL, Qdrant and Neo4j adapters
  api_intelligence/              network interception and OpenAPI generation
  free_api_integration/          public API registry and connector generation
  mobile/                        ADB automation (frame decoding is a stub)
  visual_agent/                  DOM-driven browser agent
  dashboard/                     operator console (no build step, no CDN scripts)
  eval/                          extraction eval: fixtures, scorer, runner
  deployment/                    Terraform skeleton; Helm chart metadata only
  docs/blueprint.md              target system design (includes planned parts)
  docs/demo.md                   90-second demo shot list
scripts/demo_up.sh               start datastores + server, wait for health
  tests/                         pytest suite
```

## License

MIT License — Copyright © 2026 Tanay Praveen Agrawal
