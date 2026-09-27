"""Runs the extractor over every fixture and aggregates the scores."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from byconn.eval import scoring
from byconn.pipeline import structured
from byconn.pipeline.cleaning import DataCleaner

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixtures(directory: Path = FIXTURES_DIR, only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Loads ``page.html``, ``task.json`` and ``gold.json`` from each fixture."""
    fixtures = []
    for folder in sorted(p for p in Path(directory).iterdir() if p.is_dir()):
        if only and folder.name not in only:
            continue
        fixtures.append({
            "name": folder.name,
            "html": (folder / "page.html").read_text(encoding="utf-8"),
            "task": json.loads((folder / "task.json").read_text(encoding="utf-8")),
            "gold": json.loads((folder / "gold.json").read_text(encoding="utf-8")),
        })
    return fixtures


async def run_fixture(extractor: Any, fixture: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """Extracts one fixture through the real cleaning + structured path."""
    task = fixture["task"]
    text = DataCleaner().html_to_markdown(fixture["html"])
    started = time.perf_counter()
    error = None
    try:
        result = await asyncio.wait_for(
            extractor.extract_structured(text, task["instruction"], task["schema"], task["url"]),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        result, error = structured.empty_structured(), f"timed out after {timeout:.0f}s"
    latency = time.perf_counter() - started

    score = scoring.score_records(
        result.get("data"), fixture["gold"], task["records_field"], task["key"], task["fields"],
    )
    verified, unverified = structured.count_fields(result)
    return {
        "fixture": fixture["name"],
        **score,
        "latency_s": round(latency, 2),
        "estimated_input_tokens": extractor.count_tokens(
            structured.build_prompt(text, task["instruction"], task["schema"], task["url"])
        ),
        "fields_verified": verified,
        "fields_unverified": unverified,
        "schema_valid": not result.get("schema_errors") and error is None and bool(result.get("data")),
        "windows": result.get("windows", 0),
        "error": error,
    }


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Micro-averaged field scores plus reliability and cost figures."""
    tp = sum(r["tp"] for r in rows)
    fp = sum(r["fp"] for r in rows)
    fn = sum(r["fn"] for r in rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    verified = sum(r["fields_verified"] for r in rows)
    unverified = sum(r["fields_unverified"] for r in rows)
    latencies = [r["latency_s"] for r in rows]
    return {
        "fixtures": len(rows),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "macro_f1": round(sum(r["f1"] for r in rows) / len(rows), 4) if rows else 0.0,
        "invented_records": sum(r["invented_records"] for r in rows),
        "schema_valid_rate": round(sum(r["schema_valid"] for r in rows) / len(rows), 4) if rows else 0.0,
        "citation_verified_rate": round(verified / (verified + unverified), 4) if verified + unverified else 0.0,
        "latency_p50_s": scoring.percentile(latencies, 50),
        "latency_p95_s": scoring.percentile(latencies, 95),
        "estimated_input_tokens": sum(r["estimated_input_tokens"] for r in rows),
        "errors": sum(1 for r in rows if r["error"]),
    }


def to_markdown(report: Dict[str, Any]) -> str:
    """Renders a report as the Markdown summary committed with results."""
    s, c = report["summary"], report["config"]
    lines = [
        f"# Extraction eval: {c['model']}",
        "",
        f"Run {report['run_at']} · provider `{c['provider']}` · endpoint {c['endpoint']} · "
        f"max input tokens {c['max_input_tokens']} · max windows {c['max_windows']}",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Fixtures | {s['fixtures']} |",
        f"| Field precision | {s['precision']:.1%} |",
        f"| Field recall | {s['recall']:.1%} |",
        f"| Field F1 (micro) | {s['f1']:.1%} |",
        f"| Field F1 (macro) | {s['macro_f1']:.1%} |",
        f"| Invented records | {s['invented_records']} |",
        f"| Schema-valid replies | {s['schema_valid_rate']:.1%} |",
        f"| Values backed by a verified quote | {s['citation_verified_rate']:.1%} |",
        f"| Latency p50 / p95 | {s['latency_p50_s']}s / {s['latency_p95_s']}s |",
        f"| Estimated input tokens | {s['estimated_input_tokens']:,} |",
        f"| Fixtures that errored | {s['errors']} |",
        "",
        "| Fixture | P | R | F1 | Records gold/pred/invented | Verified/unverified | Latency |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in report["fixtures"]:
        lines.append(
            f"| {r['fixture']} | {r['precision']:.2f} | {r['recall']:.2f} | {r['f1']:.2f} | "
            f"{r['gold_records']}/{r['predicted_records']}/{r['invented_records']} | "
            f"{r['fields_verified']}/{r['fields_unverified']} | {r['latency_s']}s"
            + (f" ({r['error']})" if r["error"] else "") + " |"
        )
    return "\n".join(lines) + "\n"
