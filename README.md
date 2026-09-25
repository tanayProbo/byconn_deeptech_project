# BYCONN-X: Universal AI-Powered Data Acquisition

**BYCONN-X** is an AI-native data acquisition engine designed to crawl, scrape, and understand the web at scale. It combines standard web crawling with AI-powered browser automation.

## Features

- ⚡ **Distributed Crawling:** Fast and scalable web scraping with stealth mode and proxy rotation.
- 👁️ **Visual Agent:** Uses AI (Vision-Language Models) to interact with websites — no need to write complex CSS selectors.
- 📱 **Android Automation:** Capture and automate Android apps via ADB.
- 🔍 **API Interception:** Automatically sniffs APIs and generates OpenAPI specs.
- 🧬 **Data Pipeline:** Cleans HTML, extracts entities, and creates embeddings for vector search.
- 🗄️ **Multi-DB Support:** Store data in PostgreSQL, ClickHouse, Neo4j, or Qdrant.

## Quick Setup

Make sure you have Python 3.10+ installed.

```bash
# Clone the repository
git clone https://github.com/tanayProbo/byconn_deeptech_project.git
cd byconn_deeptech_project

# Set up a virtual environment
python -m venv venv
.\venv\Scripts\activate

# Install dependencies and browsers
pip install -r requirements.txt
playwright install
```

## Environment Setup

Copy the example environment file and add your API keys:
```bash
cp .env.example .env
```
Make sure to add your `OPENAI_API_KEY` to enable the AI Extraction pipeline and Visual Agent. Database URIs (PostgreSQL, Neo4j, etc.) can also be configured here if you are using the storage adapters.

## Running the Engine

BYCONN-X comes with a fully-featured **FastAPI backend** and a **Neobrutalist UI Dashboard**.

### 1. Start the API Server
Run the main module to spin up the backend on port 8000:
```bash
python -m byconn.main
```
The server will now accept requests at `http://localhost:8000`.

### 2. Open the Dashboard
Simply open the provided HTML file in your web browser:
```bash
# On Windows
start byconn/dashboard/index.html

# On Mac
open byconn/dashboard/index.html
```
From the dashboard, you can type natural language queries, and the UI will communicate with the local API server to crawl, extract entities, and display the JSON insights live.

## CLI & Python Usage

You can also run crawls directly from the CLI:
```bash
python -m byconn.main crawl https://example.com --depth 2 --concurrency 5
```

Or programmatically in Python:

```python
from byconn.core.crawler import ByconnCrawler

crawler = ByconnCrawler(concurrency=10, stealth_mode=True)
crawler.run(start_urls=["https://example.com"])
```

Or let the AI Agent navigate for you:

```python
from byconn.visual_agent.agent_loop import VisualAgent

agent = VisualAgent(model="gpt-4o")
agent.navigate("https://example.com")
agent.act("Find the login button and click it")
```

## License

MIT License — Copyright © 2026 Tanay Praveen Agrawal
