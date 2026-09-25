import logging
import re
from collections import OrderedDict
from typing import List, Optional, Sequence, Set

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

logger = logging.getLogger("byconnx.pipeline.cleaning")

# Documents sharing more than this Jaccard overlap are treated as duplicates.
DEFAULT_DEDUP_THRESHOLD = 0.85
# Bounds memory: only the most recent N document fingerprints are retained.
DEFAULT_DEDUP_CAPACITY = 2000
# Blocks shorter than this carry too little signal for shingle comparison.
MIN_DEDUP_CHARS = 40

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# Containers whose children are treated as sibling blocks.
CONTAINERS = {
    "html", "body", "div", "section", "article", "main", "aside", "header",
    "footer", "nav", "figure", "figcaption", "blockquote", "dl", "dd", "dt",
    "form", "fieldset", "details", "summary", "address", "center",
}
LIST_TAGS = {"ul", "ol"}
SKIP_INLINE = {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}
INLINE_BOLD = {"strong", "b"}
INLINE_ITALIC = {"em", "i"}
# Inline elements that can also appear as direct children of a container.
INLINE_ELEMENTS = INLINE_BOLD | INLINE_ITALIC | {
    "a", "code", "span", "u", "mark", "small", "sub", "sup",
    "abbr", "time", "label", "q", "cite",
}


class DataCleaner:
    """
    Cleans raw web contents, converting noisy HTML trees into structured Markdown document chunks.
    Eliminates headers, sidebars, footer tags, scripts, styles, and redundant boilerplate.
    """
    def __init__(
        self,
        dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
        dedup_capacity: int = DEFAULT_DEDUP_CAPACITY,
    ):
        self.tags_to_strip = ["script", "style", "nav", "footer", "iframe", "noscript", "header"]
        # Jaccard similarity above which two documents count as the same.
        self.dedup_threshold = dedup_threshold
        self.dedup_capacity = max(1, dedup_capacity)
        # Insertion-ordered so the oldest fingerprint can be evicted first.
        self._seen_shingles: "OrderedDict[str, Set[int]]" = OrderedDict()

    # --- deduplication ------------------------------------------------------
    def reset_dedup(self) -> None:
        """Forgets every previously seen document fingerprint."""
        self._seen_shingles.clear()

    def is_duplicate(self, text: str) -> bool:
        """Reports whether ``text`` closely matches an already-seen document.

        Too-short inputs are never duplicates, since shingle overlap on a few
        words is meaningless.
        """
        signature = self.get_shingle_hash(text or "")
        if len(text or "") < MIN_DEDUP_CHARS or not signature:
            return False
        for existing in self._seen_shingles.values():
            if self._jaccard(signature, existing) >= self.dedup_threshold:
                return True
        return False

    def mark_seen(self, text: str) -> bool:
        """Records a document fingerprint.

        Returns ``False`` (without recording) when the document is a duplicate,
        so callers can use it as an atomic check-and-set.
        """
        signature = self.get_shingle_hash(text or "")
        if len(text or "") < MIN_DEDUP_CHARS or not signature:
            return True
        if self.is_duplicate(text):
            return False
        self._seen_shingles[text[:256]] = signature
        while len(self._seen_shingles) > self.dedup_capacity:
            self._seen_shingles.popitem(last=False)
        return True

    def deduplicate_blocks(self, blocks: Sequence[str]) -> List[str]:
        """Drops near-duplicate blocks from a single document.

        Boilerplate repeated across a page (nav fragments, cookie banners) is
        collapsed so it does not dilute the embedded content.
        """
        kept: List[str] = []
        seen: List[Set[int]] = []
        for block in blocks:
            candidate = (block or "").strip()
            if not candidate:
                continue
            signature = self.get_shingle_hash(candidate)
            if len(candidate) >= MIN_DEDUP_CHARS and signature:
                if any(self._jaccard(signature, other) >= self.dedup_threshold for other in seen):
                    continue
                seen.append(signature)
            kept.append(candidate)
        return kept

    @staticmethod
    def _jaccard(left: Set[int], right: Set[int]) -> float:
        """Jaccard similarity between two shingle sets."""
        if not left or not right:
            return 0.0
        intersection = len(left & right)
        if not intersection:
            return 0.0
        return intersection / len(left | right)

    def clean_html(self, html_content: str) -> str:
        """Removes layout templates, script codes, and boilerplate tags."""
        soup = BeautifulSoup(html_content or "", "html.parser")
        for tag in soup(self.tags_to_strip):
            tag.decompose()
        return str(soup)

    def html_to_markdown(self, html_content: str) -> str:
        """Renders a cleaned HTML document as block-structured Markdown.

        Headings, paragraphs, lists (including nesting), tables, links, code
        and blockquotes are preserved. Text is never silently dropped: any
        content not matched by a specific rule still contributes to the output.
        """
        cleaned = self.clean_html(html_content)
        soup = BeautifulSoup(cleaned, "html.parser")
        root = soup.body or soup

        blocks: List[str] = []
        self._render_blocks(root, blocks)
        # Collapse repeated boilerplate blocks before emitting Markdown.
        blocks = self.deduplicate_blocks(blocks)
        markdown = "\n\n".join(block for block in blocks if block.strip())
        # Lists already carry their own single newlines; collapse 3+ newlines
        # that can appear where a list abuts a following block.
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        return markdown.strip()

    # --- block rendering ----------------------------------------------------
    def _render_blocks(self, node: Tag, out: List[str]) -> None:
        """Appends each block-level child of ``node`` to ``out``."""
        for child in node.children:
            if isinstance(child, NavigableString):
                # Loose text sitting directly in a container still counts.
                text = str(child).strip()
                if text:
                    out.append(text)
                continue
            if not isinstance(child, Tag):
                continue

            name = (child.name or "").lower()

            if name in HEADINGS:
                text = self._inline(child)
                if text:
                    out.append(f"{'#' * HEADINGS[name]} {text}")
            elif name == "p":
                text = self._inline(child)
                if text:
                    out.append(text)
            elif name in LIST_TAGS:
                rendered = self._render_list(child, ordered=(name == "ol"))
                if rendered:
                    out.append(rendered)
            elif name == "table":
                rendered = self._render_table(child)
                if rendered:
                    out.append(rendered)
            elif name == "pre":
                code = child.get_text().strip()
                if code:
                    out.append(f"```\n{code}\n```")
            elif name == "blockquote":
                quoted: List[str] = []
                self._render_blocks(child, quoted)
                text = "\n".join(part for part in quoted if part.strip())
                if text:
                    out.append("\n".join(f"> {line}" for line in text.splitlines()))
            elif name == "hr":
                out.append("---")
            elif name == "br":
                continue
            elif name in SKIP_INLINE:
                continue
            elif name in CONTAINERS:
                self._render_blocks(child, out)
            elif name in INLINE_ELEMENTS:
                # A bare inline element at block level: keep its own markup, so
                # a standalone <a> still renders as a Markdown link.
                text = self._inline_node(child)
                if text:
                    out.append(text)
            else:
                # Unknown element: recurse so nested blocks are not lost.
                self._render_blocks(child, out)

    def _render_list(self, node: Tag, ordered: bool, depth: int = 0) -> str:
        """Renders a <ul>/<ol> as Markdown lines, indenting nested lists."""
        lines: List[str] = []
        ordinal = 0
        for item in node.find_all("li", recursive=False):
            nested = list(item.find_all(LIST_TAGS, recursive=False))
            # Render the item's own text without descending into nested lists.
            clone = BeautifulSoup(str(item), "html.parser")
            for child in clone.find_all(LIST_TAGS):
                child.decompose()
            text = self._inline(clone.body or clone)
            indent = "  " * depth
            if text:
                if ordered:
                    ordinal += 1
                    marker = f"{ordinal}. "
                else:
                    marker = "- "
                for index, piece in enumerate(text.splitlines()):
                    prefix = indent + marker if index == 0 else indent + "  "
                    lines.append(f"{prefix}{piece}".rstrip())
            for child in nested:
                sub = self._render_list(child, ordered=(child.name.lower() == "ol"), depth=depth + 1)
                if sub:
                    lines.append(sub)
        return "\n".join(lines)

    def _render_table(self, node: Tag) -> str:
        """Renders a <table> as a GitHub-flavoured Markdown table.

        A header row is synthesised from the first row when the table has no
        <thead>, so the pipe table stays well-formed.
        """
        rows = []
        for tr in node.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if cells:
                rows.append([self._inline(cell) for cell in cells])
        if not rows:
            return ""

        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        # Sanitise per cell (escape pipes, flatten newlines) so the table stays
        # parseable as a pipe table.
        rows = [
            [cell.replace("|", "\\|").replace("\n", " ") for cell in row]
            for row in rows
        ]

        header = rows[0]
        body = rows[1:]
        if not any(header):
            header = [f"col{i + 1}" for i in range(width)]
            body = rows

        lines = ["| " + " | ".join(header) + " |"]
        lines.append("| " + " | ".join(["---"] * width) + " |")
        for row in body:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    # --- inline rendering ---------------------------------------------------
    def _inline(self, node: Tag) -> str:
        """Renders the inline content of a node as Markdown, on one line."""
        parts: List[str] = []
        self._render_inline(node, parts)
        text = "".join(parts)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _inline_node(self, node: Tag) -> str:
        """Renders a single inline element, including its own Markdown markup."""
        name = (node.name or "").lower()
        if name == "a":
            href = (node.get("href") or "").strip()
            text = self._inline(node)
            return f"[{text}]({href})" if href and text else text
        if name in INLINE_BOLD:
            text = self._inline(node)
            return f"**{text}**" if text else ""
        if name in INLINE_ITALIC:
            text = self._inline(node)
            return f"*{text}*" if text else ""
        if name == "code":
            text = node.get_text().strip()
            return f"`{text}`" if text else ""
        return self._inline(node)

    def _render_inline(self, node, parts: List[str]) -> None:
        """Appends inline Markdown for each descendant of ``node``."""
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue

            name = (child.name or "").lower()
            if name in SKIP_INLINE:
                continue
            if name == "br":
                parts.append(" ")
            elif name in LIST_TAGS or name in CONTAINERS or name in HEADINGS or name == "table":
                # Block content inside an inline context: flatten to its text.
                parts.append(self._inline(child))
            elif name in INLINE_ELEMENTS:
                parts.append(self._inline_node(child))
            else:
                self._render_inline(child, parts)

    def html_to_unique_markdown(self, html_content: str) -> Optional[str]:
        """Converts HTML to Markdown and returns ``None`` if already seen.

        Convenience wrapper for the crawl pipelines: call
        :meth:`html_to_markdown` and let this drop documents that closely match
        one processed earlier, so repeated pages are not stored, embedded or
        sent to the LLM a second time.
        """
        markdown = self.html_to_markdown(html_content)
        if not markdown:
            return None
        if not self.mark_seen(markdown):
            logger.info("Skipping duplicate document (%d chars).", len(markdown))
            return None
        return markdown

    # --- deduplication helpers ---------------------------------------------
    def get_shingle_hash(self, text: str, k: int = 5) -> set:
        """Creates token n-gram shingles for document deduplication checks (fuzzy hashing)."""
        tokens = (text or "").lower().split()
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
