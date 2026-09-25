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

## Usage Example

Run a basic crawl:

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
