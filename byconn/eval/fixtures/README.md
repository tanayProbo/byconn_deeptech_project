# Eval fixtures

Each folder holds `page.html` (the page as fetched), `task.json` (the
instruction and JSON Schema given to the extractor, plus how to score it) and
`gold.json` (the records a careful reader would extract).

Gold records contain only what is visible in the cleaned page text the model
receives. `byconn/tests/test_eval.py` checks this for every value, so no
fixture asks for something the model cannot see.

| Fixtures | Source | Notes |
| --- | --- | --- |
| `books-*` (6) | [books.toscrape.com](https://books.toscrape.com), category listing pages | A sandbox published by Zyte for scraping practice; prices and ratings are random. Listing titles are truncated on the page, and gold keeps them as shown. |
| `quotes-*` (6) | [quotes.toscrape.com](https://quotes.toscrape.com), pages 1–6 | Same publisher and purpose. Quotations are attributed to their real authors. |
| `synthetic-*` (8) | Written for this project | Fictional companies and people. Each has a trap: a struck-through old price, filled jobs, past events, a customer quote on a team page, a "was" column, version numbers inside release notes. |

Regenerate the scraped fixtures with `python -m byconn.eval.build_fixtures`.
The synthetic ones are edited by hand.
