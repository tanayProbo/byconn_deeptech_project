# 90-second demo

A shot list for recording the demo. Everything shown is live; nothing is
staged.

**Before recording:** set an LLM in `.env` (for example `GROQ_API_KEY` with
`MODEL_NAME=llama-3.1-8b-instant`), run `scripts/demo_up.sh`, and do one
rehearsal crawl so the browser and model are warm. Restart the server before
the take if you want a cold cache.

| Time | Show | Say |
| --- | --- | --- |
| 0:00 | Dashboard, sidebar reads `ENGINE: ONLINE` and the model name | "Every datastore and the model are live; this panel reads `/api/v1/health`." |
| 0:08 | Type `books.toscrape.com extract every book title and price`, open **Output schema**, click **Product list**, choose **Deep Research**, **Launch** | "A URL, an instruction in plain words, and a JSON Schema for the shape I want." |
| 0:18 | The job card: counters and pages streaming in | "Pages stream in over server-sent events: saved to PostgreSQL, embedded into Qdrant, entities into Neo4j." |
| 0:35 | **Data** tab: the products table | "Data in exactly my schema." |
| 0:42 | Click a **source** marker; the popover shows the quote and page | "Every value carries a quote. The quote is checked against the page in code, not trusted from the model." |
| 0:52 | Scroll to an **unverified** badge if one exists | "If the model says something the page doesn't, it's flagged, not hidden." |
| 1:00 | **Graph** tab | "The knowledge graph, built from the extracted relations; every edge links back to its page." |
| 1:10 | **Download CSV**, open it | "Straight into a spreadsheet, with a sources column." |
| 1:18 | README eval table | "And we measure it: field-level precision and recall on 20 fixtures with gold answers." |
