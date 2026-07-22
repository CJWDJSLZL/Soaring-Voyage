"""Harness execution and classification metrics."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from app.grading import GradeRequest, GradingEngine, LLMUnavailableError, LLMVerdict


class LLMHarnessGrader(Protocol):
    @property
    def is_enabled(self) -> bool: ...

    async def verdict(self, request: GradeRequest) -> LLMVerdict | None: ...


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
    failures: tuple[dict[str, Any], ...]
    error_cls_accuracy: float
    calibration_error: float
    coverage_matrix: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.as_dict(),
            "failures": list(self.failures),
            "error_cls_accuracy": self.error_cls_accuracy,
            "calibration_error": self.calibration_error,
            "coverage_matrix": self.coverage_matrix,
        }


@dataclass(frozen=True)
class HarnessPrediction:
    is_correct: bool | None
    confidence: float | None


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


def _coverage_matrix(cases: list[dict[str, Any]], predictions: list[HarnessPrediction]) -> dict[str, Any]:
    matrix: dict[str, Any] = {}
    for case, prediction in zip(cases, predictions, strict=True):
        grade_key = f"grade{case['grade']}"
        type_bucket = matrix.setdefault(grade_key, {}).setdefault(case["question_type"], {})
        bucket = type_bucket.setdefault(case["difficulty"], {"total": 0, "covered": 0, "correct": 0, "accuracy": 0.0})
        bucket["total"] += 1
        if prediction.is_correct is not None:
            bucket["covered"] += 1
            bucket["correct"] += int(prediction.is_correct == bool(case["expected_correct"]))
    for grade_bucket in matrix.values():
        for type_bucket in grade_bucket.values():
            for bucket in type_bucket.values():
                bucket["accuracy"] = round(bucket["correct"] / bucket["covered"], 4) if bucket["covered"] else 0.0
    return matrix


def _calibration_error(cases: list[dict[str, Any]], predictions: list[HarnessPrediction]) -> float:
    errors = [
        abs((prediction.confidence or 0.0) - float(prediction.is_correct == bool(case["expected_correct"])))
        for case, prediction in zip(cases, predictions, strict=True)
        if prediction.is_correct is not None and prediction.confidence is not None
    ]
    return round(sum(errors) / len(errors), 4) if errors else 0.0


def _failure_details(cases: list[dict[str, Any]], predictions: list[HarnessPrediction]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "case_id": case["id"],
            "question_type": case["question_type"],
            "grade": case["grade"],
            "difficulty": case["difficulty"],
            "expected_correct": bool(case["expected_correct"]),
            "actual_correct": prediction.is_correct,
            "actual_confidence": prediction.confidence,
            "issue": "uncovered" if prediction.is_correct is None else "correctness_mismatch",
        }
        for case, prediction in zip(cases, predictions, strict=True)
        if prediction.is_correct != bool(case["expected_correct"])
    )


def _build_report(cases: list[dict[str, Any]], predictions: list[HarnessPrediction]) -> HarnessReport:
    expected = [bool(case["expected_correct"]) for case in cases]
    actual = [prediction.is_correct for prediction in predictions]
    metrics = compute_metrics(actual, expected)
    return HarnessReport(
        metrics=metrics,
        failures=_failure_details(cases, predictions),
        error_cls_accuracy=metrics.accuracy,
        calibration_error=_calibration_error(cases, predictions),
        coverage_matrix=_coverage_matrix(cases, predictions),
    )


def select_cases(
    cases: Iterable[dict[str, Any]],
    *,
    sample_rate: float = 1.0,
    grade_levels: Iterable[int] | None = None,
) -> list[dict[str, Any]]:
    """Filter and deterministically sample harness cases."""
    selected = list(cases)
    grade_filter = set(grade_levels or [])
    if grade_filter:
        selected = [case for case in selected if int(case["grade"]) in grade_filter]
    if not selected or sample_rate >= 1.0:
        return selected
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    sample_size = max(1, math.ceil(len(selected) * sample_rate))
    return sorted(
        selected,
        key=lambda case: sha256(str(case["id"]).encode("utf-8")).hexdigest(),
    )[:sample_size]


class HarnessRunner:
    def __init__(self, use_mock: bool = False) -> None:
        self.use_mock = use_mock
        self.engine = GradingEngine()

    def _predict(self, case: dict[str, Any]) -> HarnessPrediction:
        # Mock mode is deliberately local and deterministic. A real provider can be
        # integrated here without changing dataset or metrics contracts.
        if not self.use_mock:
            raise RuntimeError("real LLM runner is not configured; pass --mock")
        result = self.engine.grade(self._request_from_case(case))
        return HarnessPrediction(result.is_correct, result.confidence)

    @staticmethod
    def _request_from_case(case: dict[str, Any]) -> GradeRequest:
        return GradeRequest(
            question=case["question"],
            reference_answer=case["reference_answer"],
            student_answer=case["student_answer"],
            question_type=case["question_type"],
            grade=case["grade"],
            hint_level=case.get("hint_level", 0),
        )

    async def _predict_with_llm(self, case: dict[str, Any], llm_grader: LLMHarnessGrader) -> HarnessPrediction:
        request = self._request_from_case(case)
        try:
            verdict = await llm_grader.verdict(request)
        except (LLMUnavailableError, ValueError):
            return HarnessPrediction(None, None)
        if verdict is None:
            return HarnessPrediction(None, None)
        result = self.engine.grade(request, verdict)
        return HarnessPrediction(result.is_correct, result.confidence)

    def run(
        self,
        source: str | Path | Iterable[dict[str, Any]],
        *,
        sample_rate: float = 1.0,
        grade_levels: Iterable[int] | None = None,
    ) -> HarnessReport:
        cases = load_cases(source) if isinstance(source, (str, Path)) else list(source)
        cases = select_cases(cases, sample_rate=sample_rate, grade_levels=grade_levels)
        predictions = [self._predict(case) for case in cases]
        return _build_report(cases, predictions)

    async def run_async(
        self,
        source: str | Path | Iterable[dict[str, Any]],
        *,
        sample_rate: float = 1.0,
        grade_levels: Iterable[int] | None = None,
        llm_grader: LLMHarnessGrader | None = None,
    ) -> HarnessReport:
        if self.use_mock:
            return self.run(source, sample_rate=sample_rate, grade_levels=grade_levels)
        if llm_grader is None or not llm_grader.is_enabled:
            raise RuntimeError("real LLM runner requires a configured LLM grader")
        cases = load_cases(source) if isinstance(source, (str, Path)) else list(source)
        cases = select_cases(cases, sample_rate=sample_rate, grade_levels=grade_levels)
        predictions = [await self._predict_with_llm(case, llm_grader) for case in cases]
        return _build_report(cases, predictions)
