from __future__ import annotations

from app.grading import (
    GradeRequest,
    GradingEngine,
    LLMVerdict,
    normalize_answer,
    safe_math_equal,
)


def test_normalizes_chinese_numbers_units_choices_and_fractions() -> None:
    assert normalize_answer(" 三百七十二元 ") == "372"
    assert normalize_answer("四分之三") == "3/4"
    assert normalize_answer("0.50米") == "1/2"
    assert normalize_answer("选项（b）", question_type="choice") == "B"
    assert normalize_answer("１２．５ 千克") == "25/2"


def test_safe_sympy_math_equivalence_and_rejects_unsafe_input() -> None:
    assert safe_math_equal("24×15", "360") is True
    assert safe_math_equal("1/2", "0.5") is True
    assert safe_math_equal("(2+3)*4", "20") is True
    assert safe_math_equal("1/0", "0") is None
    assert safe_math_equal("__import__('os').system('id')", "0") is None
    assert safe_math_equal("x+1", "2") is None


def test_router_consensus_conflict_threshold_fallback_and_empty() -> None:
    engine = GradingEngine()
    req = GradeRequest(
        question="24×15=?", reference_answer="360", student_answer="三百六十", question_type="calculation", grade=3
    )

    consensus = engine.grade(req, LLMVerdict(is_correct=True, confidence=0.96))
    assert consensus.is_correct and consensus.confidence >= 0.97
    assert consensus.source == "sympy_llm_consensus"
    assert consensus.needs_review is False

    conflict = engine.grade(req, LLMVerdict(is_correct=False, confidence=0.99))
    assert conflict.is_correct and conflict.confidence == 0.75
    assert conflict.needs_review and conflict.review_reason == "sympy_llm_conflict"

    fallback = engine.grade(req, None)
    assert fallback.is_correct and fallback.confidence == 0.80
    assert fallback.source == "rule_fallback" and fallback.needs_review

    empty = engine.grade(req.model_copy(update={"student_answer": "  "}))
    assert not empty.is_correct and empty.source == "empty_answer"
    assert "没有填写" in empty.feedback


def test_hint_state_levels_zero_to_three() -> None:
    engine = GradingEngine()
    req = GradeRequest(
        question="8+7=?", reference_answer="15", student_answer="14", question_type="calculation", grade=2
    )
    expected = ["retry", "hint", "strong_hint", "solution"]
    for level, state in enumerate(expected):
        result = engine.grade(req.model_copy(update={"hint_level": level}), None)
        assert result.hint_state == state
        assert result.locked is (level == 3)
        if level < 2:
            assert "15" not in result.feedback
        if level == 3:
            assert "15" in result.feedback
