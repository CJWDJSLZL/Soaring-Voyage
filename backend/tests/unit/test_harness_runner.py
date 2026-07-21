from __future__ import annotations

import json
from pathlib import Path

from app.harness.dataset import generate_cases, write_dataset
from app.harness.runner import HarnessRunner, compute_metrics, load_cases


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
