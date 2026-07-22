from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.grading import GradeRequest, GradingEngine, LLMVerdict
from app.harness.dataset import generate_cases, write_dataset
from app.harness.runner import HarnessRunner, compute_metrics, load_cases, select_cases


class FakeLLMGrader:
    is_enabled = True

    def __init__(self) -> None:
        self.engine = GradingEngine()
        self.requests: list[GradeRequest] = []

    async def verdict(self, request: GradeRequest) -> LLMVerdict:
        self.requests.append(request)
        return LLMVerdict(is_correct=self.engine.grade(request).is_correct, confidence=0.99)


def test_generator_is_deterministic_and_exactly_180(tmp_path: Path) -> None:
    first = generate_cases()
    second = generate_cases()
    assert first == second
    assert len(first) == 180
    core = [case for case in first if case["category"] == "core"]
    edge = [case for case in first if case["category"] == "edge"]
    assert len(core) == 135 and len(edge) == 45
    assert {case["grade"] for case in core} == {1, 2, 3}
    assert {case["question_type"] for case in core} == {"calculation", "fill_blank", "choice"}
    assert {case["difficulty"] for case in core} == {"easy", "medium", "hard"}
    combinations = {(c["grade"], c["question_type"], c["difficulty"]) for c in core}
    assert all(
        sum((c["grade"], c["question_type"], c["difficulty"]) == combo for c in core) == 5 for combo in combinations
    )
    assert len({case["id"] for case in first}) == 180

    output = tmp_path / "cases.jsonl"
    write_dataset(output)
    assert load_cases(output) == first
    assert len(output.read_text(encoding="utf-8").splitlines()) == 180
    json.loads(output.read_text(encoding="utf-8").splitlines()[0])


def test_metrics_accuracy_fpr_fnr_and_coverage() -> None:
    actual = [True, True, False, False, None]
    expected = [True, False, True, False, True]
    metrics = compute_metrics(actual, expected)
    assert metrics.total == 5
    assert metrics.covered == 4
    assert metrics.accuracy == 0.5
    assert metrics.false_positive_rate == 0.5
    assert metrics.false_negative_rate == 0.5
    assert metrics.coverage == 0.8


def test_mock_runner_passes_full_dataset() -> None:
    report = HarnessRunner(use_mock=True).run(generate_cases())
    assert report.metrics.total == 180
    assert report.metrics.coverage == 1.0
    assert report.metrics.accuracy >= 0.94
    assert report.error_cls_accuracy == report.metrics.accuracy
    assert 0 <= report.calibration_error <= 1
    assert report.coverage_matrix["grade1"]["calculation"]["easy"]["total"] == 5
    assert report.coverage_matrix["grade1"]["calculation"]["easy"]["accuracy"] == 1.0
    assert report.as_dict()["coverage_matrix"]


def test_select_cases_filters_grade_levels_and_samples_deterministically() -> None:
    cases = generate_cases()
    first = select_cases(cases, sample_rate=0.25, grade_levels=[2])
    second = select_cases(cases, sample_rate=0.25, grade_levels=[2])

    assert first == second
    assert len(first) == 15
    assert {case["grade"] for case in first} == {2}


def test_runner_applies_requested_case_selection() -> None:
    report = HarnessRunner(use_mock=True).run(generate_cases(), sample_rate=0.5, grade_levels=[1])

    assert report.metrics.total == 30
    assert report.metrics.coverage == 1.0
    assert report.metrics.accuracy >= 0.94
    assert report.coverage_matrix["grade1"]


@pytest.mark.asyncio
async def test_real_runner_uses_configured_llm_grader_on_selected_cases() -> None:
    grader = FakeLLMGrader()
    report = await HarnessRunner(use_mock=False).run_async(
        generate_cases(),
        sample_rate=0.5,
        grade_levels=[1],
        llm_grader=grader,
    )

    assert report.metrics.total == 30
    assert len(grader.requests) == 30
    assert report.metrics.coverage == 1.0
    assert report.metrics.accuracy >= 0.94


@pytest.mark.asyncio
async def test_real_runner_requires_configured_llm_grader() -> None:
    with pytest.raises(RuntimeError, match="configured LLM grader"):
        await HarnessRunner(use_mock=False).run_async(generate_cases(), sample_rate=0.01)
