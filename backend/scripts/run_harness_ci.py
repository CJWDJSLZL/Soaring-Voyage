#!/usr/bin/env python3
"""Run the grading harness as a local/CI quality gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.harness.runner import HarnessRunner  # noqa: E402

DEFAULT_DATASET = BACKEND_ROOT / "harness" / "dataset" / "grading_cases.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI grading regression harness")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--mock", action="store_true", help="use deterministic local grader")
    parser.add_argument("--min-cases", type=int, default=180)
    parser.add_argument("--fail-below", type=float, default=0.94)
    parser.add_argument("--report-file", type=Path, help="write the JSON report to this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = HarnessRunner(use_mock=args.mock).run(args.dataset)
    payload = report.as_dict()
    payload["threshold"] = args.fail_below
    payload["min_cases"] = args.min_cases
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    print(rendered)
    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(rendered + "\n", encoding="utf-8")
    if report.metrics.total < args.min_cases:
        print(f"FAIL: only {report.metrics.total} cases; require {args.min_cases}", file=sys.stderr)
        return 2
    if report.metrics.accuracy < args.fail_below:
        print(f"FAIL: accuracy {report.metrics.accuracy:.4f} < {args.fail_below:.4f}", file=sys.stderr)
        return 1
    print(f"PASS: {report.metrics.total} cases, accuracy={report.metrics.accuracy:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
