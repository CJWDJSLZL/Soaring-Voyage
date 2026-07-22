from __future__ import annotations

from app.api.routes import grade
from app.grading import GradeRequest, LLMVerdict


class FakeLLMGrader:
    is_enabled = True

    def __init__(self) -> None:
        self.requests: list[GradeRequest] = []

    async def verdict(self, request: GradeRequest) -> LLMVerdict:
        self.requests.append(request)
        return LLMVerdict(is_correct=True, confidence=0.96)


class FakeRagIndexer:
    def __init__(self, context: list[dict[str, str]] | None = None, fail: bool = False) -> None:
        self.context = context or []
        self.fail = fail

    async def search_similar(self, tenant_id: str, problem: dict) -> list[dict[str, str]]:
        if self.fail:
            raise RuntimeError("qdrant unavailable")
        return self.context


def problem() -> dict:
    return {
        "problem_id": "problem-1",
        "tenant_id": "tenant-demo",
        "problem_text": "1 + 1 = ___",
        "reference_answer": "2",
        "problem_type": "arithmetic",
        "grade_level": 1,
        "difficulty": "easy",
        "solution_steps": [],
        "tags": ["addition"],
    }


async def test_grade_injects_rag_context_into_llm_request() -> None:
    llm = FakeLLMGrader()
    rag = FakeRagIndexer([{"problem_text": "2 + 2 = ___", "reference_answer": "4"}])

    result = await grade(problem(), "2", llm, rag_indexer=rag)

    assert result["is_correct"] is True
    assert llm.requests[0].rag_context == [{"problem_text": "2 + 2 = ___", "reference_answer": "4"}]


async def test_grade_silently_degrades_when_rag_retrieval_fails() -> None:
    llm = FakeLLMGrader()
    rag = FakeRagIndexer(fail=True)

    result = await grade(problem(), "2", llm, rag_indexer=rag)

    assert result["is_correct"] is True
    assert llm.requests[0].rag_context == []
