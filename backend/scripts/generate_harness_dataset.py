#!/usr/bin/env python3
"""Regenerate the committed deterministic grading dataset."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.harness.dataset import write_dataset  # noqa: E402

if __name__ == "__main__":
    output = write_dataset(BACKEND_ROOT / "harness" / "dataset" / "grading_cases.jsonl")
    print(output)
