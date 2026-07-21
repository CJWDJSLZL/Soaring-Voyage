"""Harness execution and classification metrics."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.grading import GradeRequest, GradingEngine


@dataclass(frozen=True)
class HarnessMetrics:
    total: int
    covered: int
    accuracy: float
    false_positive_rate: float
    false_negative_rate: float
    coverage: float

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True)
class HarnessReport:
    metrics: HarnessMetrics
    failures: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"metrics": self.metrics.as_dict(), "failures": list(self.failures)}


def load_cases(source: str | Path) -> list[dict[str, Any]]:
    path = Path(source)
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    cases: list[dict[str, Any]] = []
    for file_path in files:
        for line_number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {file_path}:{line_number}") from exc
    return cases


def compute_metrics(actual: Iterable[bool | None], expected: Iterable[bool]) -> HarnessMetrics:
    pairs = list(zip(actual, expected, strict=True))
    covered = [(prediction, truth) for prediction, truth in pairs if prediction is not None]
    correct = sum(prediction == truth for prediction, truth in covered)
    fp = sum(prediction is True and truth is False for prediction, truth in covered)
    negatives = sum(truth is False for _, truth in covered)
    fn = sum(prediction is False and truth is True for prediction, truth in covered)
    positives = sum(truth is True for _, truth in covered)
    return HarnessMetrics(
        total=len(pairs),
        covered=len(covered),
        accuracy=correct / len(covered) if covered else 0.0,
        false_positive_rate=fp / negatives if negatives else 0.0,
        false_negative_rate=fn / positives if positives else 0.0,
        coverage=len(covered) / len(pairs) if pairs else 0.0,
    )


class HarnessRunner:
    def __init__(self, use_mock: bool = False) -> None:
        self.use_mock = use_mock
        self.engine = GradingEngine()

    def _predict(self, case: dict[str, Any]) -> bool | None:
        # Mock mode is deliberately local and deterministic. A real provider can be
        # integrated here without changing dataset or metrics contracts.
        if not self.use_mock:
            raise RuntimeError("real LLM runner is not configured; pass --mock")
        request = GradeRequest(
            question=case["question"],
            reference_answer=case["reference_answer"],
            student_answer=case["student_answer"],
            question_type=case["question_type"],
            grade=case["grade"],
            hint_level=case.get("hint_level", 0),
        )
        return self.engine.grade(request).is_correct

    def run(self, source: str | Path | Iterable[dict[str, Any]]) -> HarnessReport:
        cases = load_cases(source) if isinstance(source, (str, Path)) else list(source)
        predictions = [self._predict(case) for case in cases]
        expected = [bool(case["expected_correct"]) for case in cases]
        failures = tuple(
            case["id"]
            for case, prediction, truth in zip(cases, predictions, expected, strict=True)
            if prediction != truth
        )
        return HarnessReport(compute_metrics(predictions, expected), failures)
