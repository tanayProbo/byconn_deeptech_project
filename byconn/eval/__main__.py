"""Command line: ``python -m byconn.eval [--model NAME] [--only FIXTURE ...]``.

Uses the same provider configuration as the server (OPENAI_API_KEY,
OPENAI_API_BASE, GROQ_API_KEY, MODEL_NAME...). With no LLM configured it
exits without writing anything: numbers come only from a real run.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from byconn.eval import runner

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _endpoint_label(base_url):
    """Host only, so a results file never records a URL with credentials."""
    if not base_url:
        return "provider default"
    host = urlparse(base_url).hostname or "?"
    return "local" if host in {"localhost", "127.0.0.1", "::1"} else host


async def _main(args) -> int:
    if args.model:
        os.environ["MODEL_NAME"] = args.model
    from byconn.pipeline.llm_extractor import LLMExtractor

    extractor = LLMExtractor(cache_size=0)
    if not extractor.is_available:
        print("No LLM is configured, so there is nothing to evaluate. Set GROQ_API_KEY, "
              "OPENAI_API_KEY, or OPENAI_API_KEY=ollama for a local model.", file=sys.stderr)
        return 2

    fixtures = runner.load_fixtures(only=args.only)
    rows = []
    for fixture in fixtures:
        row = await runner.run_fixture(extractor, fixture, timeout=args.timeout)
        rows.append(row)
        print(f"{row['fixture']:24} F1 {row['f1']:.2f}  {row['latency_s']:>6}s"
              + (f"  {row['error']}" if row["error"] else ""), flush=True)

    run_at = datetime.now(timezone.utc).replace(microsecond=0)
    report = {
        "run_at": run_at.isoformat(),
        "config": {
            "model": extractor.model,
            "provider": extractor.provider,
            "endpoint": _endpoint_label(extractor.base_url),
            "max_input_tokens": extractor.max_input_tokens,
            "max_windows": extractor.max_windows,
            "temperature": extractor.temperature,
        },
        "summary": runner.aggregate(rows),
        "fixtures": rows,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{run_at:%Y-%m-%d}-{re.sub(r'[^A-Za-z0-9.-]+', '_', extractor.model)}"
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = runner.to_markdown(report)
    (out / f"{stem}.md").write_text(markdown, encoding="utf-8")
    print("\n" + markdown)
    print(f"Wrote {out / stem}.json and .md")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m byconn.eval", description=__doc__)
    parser.add_argument("--model", help="model id (sets MODEL_NAME)")
    parser.add_argument("--only", nargs="*", help="fixture names to run")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds per fixture")
    parser.add_argument("--out", default=str(RESULTS_DIR), help="results directory")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
