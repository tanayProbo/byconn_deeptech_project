"""Field-level scoring of extracted records against gold records.

A fixture's gold is a list of records under one field (``products``,
``quotes``...). Predicted records are paired with gold records by a key field,
then every field is compared:

* true positive: a predicted value that matches the gold value;
* false positive: a predicted non-empty value that is wrong, or any value in
  a predicted record with no gold counterpart (an invented record);
* false negative: a gold value that was not predicted correctly.

Values are compared after normalisation (case, Unicode, typography,
whitespace, surrounding quotes, a trailing ellipsis). Values that carry a
number (prices, years) must agree numerically, so "£51.77" matches "51.77"
but not "£51.70".
"""

import math
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

_TYPOGRAPHY = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ", "…": "...",
})
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def normalize(value: Any) -> str:
    """Canonical text form used for comparisons."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).translate(_TYPOGRAPHY)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("\"'").strip()
    text = re.sub(r"\s*\.\.\.$", "", text).strip()
    return text.casefold()


def number_of(value: Any) -> Optional[float]:
    """The single number a value carries, or None if it has none or several."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = _NUMBER.findall(str(value or ""))
    if len(found) != 1:
        return None
    try:
        return float(found[0].replace(",", ""))
    except ValueError:
        return None


def values_match(predicted: Any, gold: Any) -> bool:
    """Whether a predicted value counts as the gold value."""
    gold_number = number_of(gold)
    if gold_number is not None and not re.search(r"[A-Za-z]{3,}", str(gold)):
        predicted_number = number_of(predicted)
        return predicted_number is not None and abs(predicted_number - gold_number) < 0.005
    return bool(normalize(gold)) and normalize(predicted) == normalize(gold)


def _present(value: Any) -> bool:
    return value is not None and normalize(value) != ""


def pair_records(
    predicted: Sequence[Dict[str, Any]],
    gold: Sequence[Dict[str, Any]],
    key: str,
) -> Tuple[List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]], List[Dict[str, Any]]]:
    """Pairs each gold record with the first unused predicted record whose key
    matches. Returns ``(pairs, unmatched_predictions)``."""
    unused = [p for p in predicted if isinstance(p, dict)]
    pairs = []
    for record in gold:
        match = next((p for p in unused if values_match(p.get(key), record.get(key))), None)
        if match is not None:
            unused.remove(match)
        pairs.append((record, match))
    return pairs, unused


def score_records(
    predicted_data: Any,
    gold_records: Sequence[Dict[str, Any]],
    records_field: str,
    key: str,
    fields: Sequence[str],
) -> Dict[str, Any]:
    """Scores one fixture. Returns counts plus precision, recall and F1."""
    predicted = []
    if isinstance(predicted_data, dict) and isinstance(predicted_data.get(records_field), list):
        predicted = predicted_data[records_field]
    pairs, extras = pair_records(predicted, gold_records, key)

    tp = fp = fn = 0
    for gold, guess in pairs:
        for field in fields:
            gold_value = gold.get(field)
            guess_value = guess.get(field) if guess else None
            if _present(gold_value) and _present(guess_value) and values_match(guess_value, gold_value):
                tp += 1
                continue
            if _present(gold_value):
                fn += 1
            if _present(guess_value):
                fp += 1
    for extra in extras:
        fp += sum(1 for field in fields if _present(extra.get(field)))

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "gold_records": len(gold_records),
        "predicted_records": len([p for p in predicted if isinstance(p, dict)]),
        "matched_records": sum(1 for _, guess in pairs if guess is not None),
        "invented_records": len(extras),
    }


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile; None for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, min(len(ordered), math.ceil(q / 100 * len(ordered))))
    return ordered[rank - 1]
