"""Schema-driven extraction support: validation, citation checks and merging.

:meth:`~byconn.pipeline.llm_extractor.LLMExtractor.extract_structured` asks a
model for data matching a user's JSON Schema, with a verbatim quote from the
page behind every value. This module holds the model-independent parts, so the
rules that keep results honest can be tested without an LLM:

* a quote counts only if it really occurs in the page text
  (:func:`verify_citations`); a value with no verified quote is reported as
  unverified rather than silently trusted;
* long pages are split into windows (:func:`split_windows`) whose results are
  merged (:func:`merge_structured`) with their citations re-based onto the
  merged document.

Citations are keyed by RFC 6901 JSON pointers into ``data``, e.g.
``/plans/0/price``.
"""

import copy
import json
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Used when a job gives an instruction but no schema.
DEFAULT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object"}},
    },
}

MAX_SCHEMA_BYTES = 20_000
MAX_QUOTE_CHARS = 300
# Short values ("1", "no") occur on almost any page, so finding one verbatim
# proves nothing. Longer values found verbatim count as their own evidence.
MIN_SELF_EVIDENT_CHARS = 4

STRUCTURED_PROMPT_TEMPLATE = """You extract data from a web page into JSON that matches a JSON Schema.

INSTRUCTION:
<<instruction>>

SOURCE URL: <<url>>

JSON SCHEMA:
<<schema>>

PAGE TEXT:
\"\"\"
<<text>>
\"\"\"

Reply with ONE JSON object only, shaped exactly like:
{"data": <object matching the schema>,
 "citations": {"<JSON pointer into data>": ["<verbatim quote from the page>"]}}

Rules:
- Use only facts stated in the page text. If a value is not on the page, use
  null or leave it out. Never guess or use outside knowledge.
- For every value you fill in, add a citation. Its key is the JSON pointer of
  that value inside "data" (for example "/plans/0/price"). Its quote is copied
  character for character from the page text, at most 200 characters.
"""


# --- schema ------------------------------------------------------------------
def validate_schema(schema: Any) -> Dict[str, Any]:
    """Checks a user-supplied JSON Schema and returns it.

    Raises:
        ValueError: if it is not a valid schema for a JSON object, or is too big.
    """
    from jsonschema import exceptions, validators

    if not isinstance(schema, dict):
        raise ValueError("schema must be a JSON object")
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        raise ValueError(f"schema must be at most {MAX_SCHEMA_BYTES} bytes")
    if schema.get("type") != "object":
        raise ValueError('schema must describe an object: set "type": "object"')
    try:
        validators.validator_for(schema).check_schema(schema)
    except exceptions.SchemaError as exc:
        raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
    return schema


def schema_errors(data: Any, schema: Dict[str, Any], limit: int = 10) -> List[str]:
    """Returns human-readable validation errors of ``data`` against ``schema``."""
    from jsonschema import validators

    validator = validators.validator_for(schema)(schema)
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        where = "/" + "/".join(escape_token(str(p)) for p in error.path)
        errors.append(f"{where}: {error.message}")
        if len(errors) >= limit:
            break
    return errors


def build_prompt(text: str, instruction: str, schema: Dict[str, Any], url: str) -> str:
    """Renders the extraction prompt. Uses markers, not str.format, because
    the schema and page text are full of braces."""
    return (
        STRUCTURED_PROMPT_TEMPLATE
        .replace("<<instruction>>", instruction or "Extract the data described by the schema.")
        .replace("<<url>>", url or "(unknown)")
        .replace("<<schema>>", json.dumps(schema, indent=2))
        .replace("<<text>>", text)
    )


# --- JSON pointers -------------------------------------------------------------
def escape_token(token: str) -> str:
    """Escapes one JSON-pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def leaf_pointers(value: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    """Lists ``(pointer, value)`` for every non-null scalar in ``value``."""
    if isinstance(value, dict):
        out: List[Tuple[str, Any]] = []
        for key, item in value.items():
            out.extend(leaf_pointers(item, f"{prefix}/{escape_token(str(key))}"))
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            out.extend(leaf_pointers(item, f"{prefix}/{index}"))
        return out
    if value is None or value == "":
        return []
    return [(prefix, value)]


def normalize_pointer(raw: Any) -> Optional[str]:
    """Coerces the pointer spellings models produce into RFC 6901 form.

    Accepts ``/plans/0/price``, ``plans.0.price``, ``plans[0].price`` and the
    same with a leading ``data`` segment.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if not text.startswith("/"):
        text = re.sub(r"\[(\d+)\]", r".\1", text)
        text = "/" + "/".join(escape_token(part) for part in text.split(".") if part)
    if text == "/data":
        return ""
    if text.startswith("/data/"):
        text = text[len("/data"):]
    return text


# --- citation verification -----------------------------------------------------
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"[*_`#>|]+")
_TYPOGRAPHY = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
})


def normalize_text(text: str) -> str:
    """Normalizes text so a quote matches the page despite formatting.

    Folds case, Unicode forms, smart quotes and dashes, strips Markdown markup
    (the page text is cleaned Markdown, the model often drops the syntax) and
    collapses whitespace.
    """
    text = unicodedata.normalize("NFKC", str(text)).translate(_TYPOGRAPHY)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _MARKUP.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _quotes_from(raw: Any) -> List[str]:
    """Accepts a quote given as a string, a list, or {"quote": ...} objects."""
    items = raw if isinstance(raw, list) else [raw]
    quotes = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("quote") or item.get("text")
        if isinstance(item, (str, int, float)) and str(item).strip():
            quotes.append(str(item).strip()[:MAX_QUOTE_CHARS])
    return quotes


def verify_citations(
    data: Any,
    raw_citations: Any,
    page_text: str,
    url: str,
) -> Tuple[Dict[str, List[Dict[str, str]]], List[str]]:
    """Keeps only quotes that really occur on the page.

    Returns ``(citations, unverified)``. ``citations`` maps each leaf pointer
    of ``data`` to its verified quotes. A leaf with no verified quote of its
    own is still verified if its value is long enough to be self-evident
    (:data:`MIN_SELF_EVIDENT_CHARS`) and occurs verbatim on the page.
    Everything else is listed in ``unverified``. Fabricated quotes are dropped.
    """
    haystack = normalize_text(page_text or "")
    offered: Dict[str, List[str]] = {}
    if isinstance(raw_citations, dict):
        for key, raw in raw_citations.items():
            pointer = normalize_pointer(key)
            if pointer is not None:
                offered.setdefault(pointer, []).extend(_quotes_from(raw))

    citations: Dict[str, List[Dict[str, str]]] = {}
    unverified: List[str] = []
    for pointer, value in leaf_pointers(data):
        kept = []
        for quote in offered.get(pointer, []):
            needle = normalize_text(quote)
            # Shown to people: the page's words, not "[title](catalogue/...)".
            readable = re.sub(r"\s+", " ", _MARKDOWN_LINK.sub(r"\1", quote)).strip()
            if needle and needle in haystack and readable not in {k["quote"] for k in kept}:
                kept.append({"quote": readable, "url": url})
        if not kept:
            as_text = str(value).strip()
            needle = normalize_text(as_text)
            if len(needle) >= MIN_SELF_EVIDENT_CHARS and needle in haystack:
                kept.append({"quote": as_text[:MAX_QUOTE_CHARS], "url": url})
        if kept:
            citations[pointer] = kept
        else:
            unverified.append(pointer)
    return citations, unverified


# --- windowing -----------------------------------------------------------------
def split_windows(
    text: str,
    max_tokens: int,
    count_tokens: Callable[[str], int],
    max_windows: int,
) -> List[str]:
    """Splits text on paragraph boundaries into windows within a token budget.

    At most ``max_windows`` windows are returned; the rest of a very long page
    is not sent to the model (bounded cost), which the caller reports.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    windows: List[str] = []
    current: List[str] = []
    used = 0
    for paragraph in paragraphs:
        cost = count_tokens(paragraph)
        if cost > max_tokens:
            # One enormous paragraph: hard-cut it by characters (~4/token).
            if current:
                windows.append("\n\n".join(current))
                current, used = [], 0
            step = max_tokens * 4
            windows.extend(paragraph[i:i + step] for i in range(0, len(paragraph), step))
            continue
        if used + cost > max_tokens and current:
            windows.append("\n\n".join(current))
            current, used = [], 0
        current.append(paragraph)
        used += cost
    if current:
        windows.append("\n\n".join(current))
    return windows[:max(1, max_windows)]


# --- merging -------------------------------------------------------------------
def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _map_subtree(value: Any, source: str, target: str, leafmap: Dict[str, Optional[str]]) -> None:
    for suffix, _ in leaf_pointers(value):
        leafmap[source + suffix] = target + suffix


def _drop_subtree(value: Any, source: str, leafmap: Dict[str, Optional[str]]) -> None:
    for suffix, _ in leaf_pointers(value):
        leafmap[source + suffix] = None


def _merge_value(
    merged: Any,
    incoming: Any,
    src: str,
    dst: str,
    leafmap: Dict[str, Optional[str]],
    unverified_now: set,
    incoming_unverified: set,
    citations: Dict[str, List[Dict[str, str]]],
) -> Any:
    if merged is None or merged == "":
        _map_subtree(incoming, src, dst, leafmap)
        return copy.deepcopy(incoming)
    if incoming is None or incoming == "":
        return merged
    if isinstance(merged, dict) and isinstance(incoming, dict):
        for key, value in incoming.items():
            token = escape_token(str(key))
            merged[key] = _merge_value(
                merged.get(key), value, f"{src}/{token}", f"{dst}/{token}",
                leafmap, unverified_now, incoming_unverified, citations,
            )
        return merged
    if isinstance(merged, list) and isinstance(incoming, list):
        seen = [_canonical(item) for item in merged]
        for index, item in enumerate(incoming):
            key = _canonical(item)
            if key in seen:
                # Duplicate: its citations reinforce the existing item.
                _map_subtree(item, f"{src}/{index}", f"{dst}/{seen.index(key)}", leafmap)
            else:
                merged.append(copy.deepcopy(item))
                seen.append(key)
                _map_subtree(item, f"{src}/{index}", f"{dst}/{len(merged) - 1}", leafmap)
        return merged
    if isinstance(merged, (dict, list)) or isinstance(incoming, (dict, list)):
        _drop_subtree(incoming, src, leafmap)  # shape conflict: keep the first
        return merged
    # Two scalars. The first value wins, unless it is unverified and the new
    # one is not: a value backed by a quote beats one that is not.
    if merged == incoming:
        leafmap[src] = dst
        return merged
    if dst in unverified_now and src not in incoming_unverified:
        unverified_now.discard(dst)
        citations.pop(dst, None)
        leafmap[src] = dst
        return incoming
    leafmap[src] = None
    return merged


def merge_structured(parts: Iterable[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Merges several ``{data, citations, unverified}`` results into one.

    Arrays concatenate with duplicates removed; objects merge key by key; for
    conflicting scalars the first value wins unless only a later one is
    verified. Citations and unverified pointers are re-based onto the merged
    document.
    """
    data: Any = None
    citations: Dict[str, List[Dict[str, str]]] = {}
    unverified: set = set()
    errors: List[str] = []
    for part in parts:
        if not part:
            continue
        leafmap: Dict[str, Optional[str]] = {}
        incoming_unverified = set(part.get("unverified") or [])
        data = _merge_value(
            data, part.get("data"), "", "", leafmap,
            unverified, incoming_unverified, citations,
        )
        for pointer, quotes in (part.get("citations") or {}).items():
            target = leafmap.get(pointer)
            if not target and target != "":
                continue
            existing = citations.setdefault(target, [])
            for quote in quotes:
                if quote not in existing:
                    existing.append(quote)
        for pointer in incoming_unverified:
            target = leafmap.get(pointer)
            if target is not None and target not in citations:
                unverified.add(target)
        errors.extend(part.get("schema_errors") or [])
    unverified -= set(citations)
    return {
        "data": data if data is not None else {},
        "citations": citations,
        "unverified": sorted(unverified),
        "schema_errors": errors,
    }


def empty_structured() -> Dict[str, Any]:
    """The canonical empty structured result."""
    return {"data": {}, "citations": {}, "unverified": [], "schema_errors": []}


def count_fields(result: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    """Returns ``(verified, unverified)`` leaf counts of a structured result."""
    if not result:
        return 0, 0
    return len(result.get("citations") or {}), len(result.get("unverified") or [])


__all__: Sequence[str] = [
    "DEFAULT_SCHEMA", "validate_schema", "schema_errors", "build_prompt",
    "leaf_pointers", "normalize_pointer", "normalize_text", "verify_citations",
    "split_windows", "merge_structured", "empty_structured", "count_fields",
]
