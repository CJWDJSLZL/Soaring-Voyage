"""Core grading router and hint-loop state transition."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .mathcheck import safe_math_equal
from .normalization import normalize_answer

QuestionType = Literal["calculation", "fill_blank", "choice"]
HintState = Literal["retry", "hint", "strong_hint", "solution", "completed"]
REVIEW_THRESHOLD = 0.85


class GradeRequest(BaseModel):
    question: str
    reference_answer: str
    student_answer: str
    question_type: QuestionType
    grade: int = Field(ge=1, le=6)
    hint_level: int = Field(default=0, ge=0, le=3)
    solution_steps: list[str] = Field(default_factory=list, max_length=20)


class LLMVerdict(BaseModel):
    is_correct: bool
    confidence: float = Field(ge=0, le=1)
    feedback: str | None = None


class GradeResult(BaseModel):
    is_correct: bool
    confidence: float
    source: str
    needs_review: bool
    review_reason: str | None = None
    feedback: str
    hint_level: int
    hint_state: HintState
    locked: bool
    normalized_student_answer: str
    normalized_reference_answer: str
    agent_trace: list[dict[str, object]]


def _deterministic_verdict(request: GradeRequest, student: str, reference: str) -> bool | None:
    if not student:
        return False
    if request.question_type == "choice":
        return student == reference
    mathematical = safe_math_equal(student, reference)
    return mathematical if mathematical is not None else student.casefold() == reference.casefold()


def _hint_feedback(request: GradeRequest, is_correct: bool) -> tuple[str, HintState, bool]:
    if is_correct:
        return "答对啦，继续保持！", "completed", False
    if request.hint_level == 0:
        return "再检查一下计算过程，你一定能找到问题。", "retry", False
    if request.hint_level == 1:
        return "想一想应该先算哪一步，再试一次。", "hint", False
    if request.hint_level == 2:
        return "你这道题差一点点。把每一步写出来并重新核对。", "strong_hint", False
    steps = "；".join(request.solution_steps)
    detail = f"解题步骤：{steps}。" if steps else ""
    return f"完整答案是 {request.reference_answer}。{detail}请对照步骤再理解一次。", "solution", True


class GradingEngine:
    """Route deterministic and optional LLM verdicts with auditable confidence rules."""

    review_threshold: float

    def __init__(self, review_threshold: float = REVIEW_THRESHOLD) -> None:
        self.review_threshold = review_threshold

    def grade(self, request: GradeRequest, llm_verdict: LLMVerdict | None = None) -> GradeResult:
        student = normalize_answer(request.student_answer, request.question_type)
        reference = normalize_answer(request.reference_answer, request.question_type)
        trace: list[dict[str, object]] = [
            {"node": "Parser", "student": student, "reference": reference},
        ]
        if not student:
            feedback, state, locked = _hint_feedback(request, False)
            return GradeResult(
                is_correct=False,
                confidence=1.0,
                source="empty_answer",
                needs_review=False,
                feedback=feedback if locked else "你还没有填写答案哦",
                hint_level=request.hint_level,
                hint_state=state,
                locked=locked,
                normalized_student_answer=student,
                normalized_reference_answer=reference,
                agent_trace=trace + [{"node": "Router", "route": "empty_answer"}],
            )

        rule = _deterministic_verdict(request, student, reference)
        trace.append({"node": "SymPy", "success": rule is not None, "is_correct": rule})
        reason: str | None
        if llm_verdict is None:
            is_correct = bool(rule)
            confidence, source, reason = 0.80, "rule_fallback", "low_confidence"
            trace.extend(
                (
                    {"node": "LLM", "status": "unavailable"},
                    {"node": "Router", "route": source},
                )
            )
        elif rule is None:
            is_correct = llm_verdict.is_correct
            confidence, source = llm_verdict.confidence, "llm_only"
            reason = "low_confidence" if confidence < self.review_threshold else None
            trace.extend(
                (
                    {"node": "LLM", "is_correct": is_correct, "confidence": confidence},
                    {"node": "Router", "route": source},
                )
            )
        elif rule == llm_verdict.is_correct:
            is_correct = rule
            confidence, source, reason = max(0.97, llm_verdict.confidence), "sympy_llm_consensus", None
            trace.extend(
                (
                    {"node": "LLM", "is_correct": llm_verdict.is_correct, "confidence": llm_verdict.confidence},
                    {"node": "Router", "route": source},
                )
            )
        else:
            is_correct = rule
            confidence, source, reason = 0.75, "sympy_llm_conflict", "sympy_llm_conflict"
            trace.extend(
                (
                    {"node": "LLM", "is_correct": llm_verdict.is_correct, "confidence": llm_verdict.confidence},
                    {"node": "Router", "route": source},
                )
            )

        feedback, state, locked = _hint_feedback(request, is_correct)
        if llm_verdict and llm_verdict.feedback and is_correct:
            feedback = llm_verdict.feedback
        needs_review = confidence < self.review_threshold
        trace.append({"node": "Feedback", "hint_level": request.hint_level, "state": state})
        return GradeResult(
            is_correct=is_correct,
            confidence=confidence,
            source=source,
            needs_review=needs_review,
            review_reason=reason if needs_review else None,
            feedback=feedback,
            hint_level=request.hint_level,
            hint_state=state,
            locked=locked,
            normalized_student_answer=student,
            normalized_reference_answer=reference,
            agent_trace=trace,
        )


def route_grade(request: GradeRequest, llm_verdict: LLMVerdict | None = None) -> GradeResult:
    """Convenience entry point for API/task workers."""
    return GradingEngine().grade(request, llm_verdict)
