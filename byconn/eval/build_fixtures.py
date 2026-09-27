"""Rebuilds the scraped fixtures from the two public scraping sandboxes.

    python -m byconn.eval.build_fixtures

books.toscrape.com and quotes.toscrape.com are published by Zyte for
practising web scraping. Gold records are read from each page's markup and
hold only what is visible in the page text the extractor receives (for
example the truncated book titles shown in listings). The synthetic fixtures
are written by hand and are not touched by this script.
"""

import json
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

FIXTURES = Path(__file__).resolve().parent / "fixtures"
USER_AGENT = "ByconnXBot-eval (+https://github.com/tanayProbo/byconn_deeptech_project)"

BOOK_CATEGORIES = {
    "books-poetry": "poetry_23",
    "books-travel": "travel_2",
    "books-mystery": "mystery_3",
    "books-history": "history_32",
    "books-humor": "humor_30",
    "books-science": "science_22",
}
QUOTE_PAGES = {f"quotes-page-{n}": n for n in range(1, 7)}

BOOK_TASK = {
    "instruction": "Extract every book listed on this catalogue page with its title and price.",
    "schema": {
        "type": "object",
        "properties": {"products": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
            "required": ["name"]}}},
    },
    "records_field": "products",
    "key": "name",
    "fields": ["name", "price"],
}
QUOTE_TASK = {
    "instruction": "Extract every quotation on this page with its author.",
    "schema": {
        "type": "object",
        "properties": {"quotes": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "author": {"type": "string"}},
            "required": ["text"]}}},
    },
    "records_field": "quotes",
    "key": "text",
    "fields": ["text", "author"],
}


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def write(name: str, url: str, html: str, task: dict, gold: list) -> None:
    folder = FIXTURES / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page.html").write_text(html, encoding="utf-8")
    (folder / "task.json").write_text(json.dumps({"url": url, **task}, indent=2) + "\n", encoding="utf-8")
    (folder / "gold.json").write_text(json.dumps(gold, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{name}: {len(gold)} records")


def main() -> None:
    for name, slug in BOOK_CATEGORIES.items():
        url = f"https://books.toscrape.com/catalogue/category/books/{slug}/index.html"
        html = fetch(url)
        soup = BeautifulSoup(html, "html.parser")
        gold = [
            {"name": pod.h3.a.get_text(strip=True), "price": pod.select_one(".price_color").get_text(strip=True)}
            for pod in soup.select("article.product_pod")
        ]
        write(name, url, html, BOOK_TASK, gold)

    for name, number in QUOTE_PAGES.items():
        url = f"https://quotes.toscrape.com/page/{number}/"
        html = fetch(url)
        soup = BeautifulSoup(html, "html.parser")
        gold = [
            {"text": q.select_one(".text").get_text(strip=True).strip("“”"),
             "author": q.select_one(".author").get_text(strip=True)}
            for q in soup.select("div.quote")
        ]
        write(name, url, html, QUOTE_TASK, gold)


if __name__ == "__main__":
    main()
