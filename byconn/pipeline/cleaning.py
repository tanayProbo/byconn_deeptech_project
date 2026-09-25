import re
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger("byconnx.pipeline.cleaning")

class DataCleaner:
    """
    Cleans raw web contents, converting noisy HTML trees into structured Markdown document chunks.
    Eliminates headers, sidebars, footer tags, scripts, styles, and redundant boilerplate.
    """
    def __init__(self):
        self.tags_to_strip = ["script", "style", "nav", "footer", "iframe", "noscript", "header"]

    def clean_html(self, html_content: str) -> str:
        """Removes layout templates, script codes, and boilerplate tags."""
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(self.tags_to_strip):
            tag.decompose()
        return str(soup)

    def html_to_markdown(self, html_content: str) -> str:
        """Renders raw text nodes into structured Markdown format."""
        cleaned = self.clean_html(html_content)
        soup = BeautifulSoup(cleaned, "html.parser")
        
        # Simplistic parser mapping heading tags to markdown headers
        markdown_lines = []
        for elem in soup.descendants:
            if elem.name in ["h1", "h2", "h3"]:
                markdown_lines.append(f"\n\n{'#' * int(elem.name[1])} {elem.get_text().strip()}\n")
            elif elem.name == "p":
                markdown_lines.append(f"\n{elem.get_text().strip()}\n")
            elif elem.name == "li":
                markdown_lines.append(f"- {elem.get_text().strip()}")
            elif elem.name == "a":
                href = elem.get("href", "")
                text = elem.get_text().strip()
                if href and text:
                    markdown_lines.append(f" [{text}]({href}) ")

        raw_md = "".join(markdown_lines)
        # Collapse multiple newlines down
        return re.sub(r'\n{3,}', '\n\n', raw_md).strip()

    def get_shingle_hash(self, text: str, k: int = 5) -> set:
        """Creates token n-gram shingles for document deduplication checks (fuzzy hashing)."""
        tokens = text.lower().split()
        shingles = set()
        for i in range(len(tokens) - k + 1):
            shingle = " ".join(tokens[i:i+k])
            shingles.add(hash(shingle))
        return shingles

    def compute_jaccard_similarity(self, text_a: str, text_b: str) -> float:
        """Computes Jaccard Similarity index to detect duplicate content chunks."""
        hash_a = self.get_shingle_hash(text_a)
        hash_b = self.get_shingle_hash(text_b)
        
        if not hash_a or not hash_b:
            return 0.0
            
        intersection = hash_a.intersection(hash_b)
        union = hash_a.union(hash_b)
        return len(intersection) / len(union)
