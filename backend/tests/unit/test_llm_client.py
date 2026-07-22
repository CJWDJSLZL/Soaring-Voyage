from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.grading import GradeRequest
from app.grading.llm_client import DeepSeekGradingClient, LLMClientConfig


def request(**overrides):
    data = {
        "question": "1 + 1 = ?",
        "reference_answer": "2",
        "student_answer": "2",
        "question_type": "calculation",
        "grade": 1,
    }
    data.update(overrides)
    return GradeRequest(**data)


def response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def config(**overrides):
    data = {
        "api_key": "test-key",
        "base_url": "https://api.deepseek.com/v1",
        "primary_model": "deepseek-v4-flash",
        "fallback_model": "deepseek-v4-pro",
        "max_retries": 1,
        "timeout_seconds": 30,
        "use_mock": False,
    }
    data.update(overrides)
    return LLMClientConfig(**data)


@pytest.mark.asyncio
async def test_unconfigured_or_mock_client_does_not_call_network() -> None:
    fake_client = SimpleNamespace(
        close=AsyncMock(), chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))
    )
    unconfigured = DeepSeekGradingClient(config(api_key=None), client=fake_client)
    mock = DeepSeekGradingClient(config(use_mock=True), client=fake_client)

    assert await unconfigured.verdict(request()) is None
    assert await mock.verdict(request()) is None
    assert fake_client.chat.completions.create.await_count == 0
    assert unconfigured.health_status == "unconfigured"
    assert mock.health_status == "mock"


@pytest.mark.asyncio
async def test_primary_model_failure_falls_back_to_secondary_model() -> None:
    create = AsyncMock(
        side_effect=[
            response("not-json"),
            response('{"is_correct": true, "confidence": 0.96, "feedback": "答对啦"}'),
        ]
    )
    fake_client = SimpleNamespace(close=AsyncMock(), chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    grader = DeepSeekGradingClient(config(), client=fake_client)

    verdict = await grader.verdict(request())

    assert verdict is not None
    assert verdict.is_correct is True
    assert verdict.confidence == 0.96
    assert [call.kwargs["model"] for call in create.await_args_list] == ["deepseek-v4-flash", "deepseek-v4-pro"]


@pytest.mark.asyncio
async def test_uuid_like_payload_is_rejected_before_llm_call() -> None:
    create = AsyncMock()
    fake_client = SimpleNamespace(close=AsyncMock(), chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    grader = DeepSeekGradingClient(config(), client=fake_client)

    with pytest.raises(ValueError, match="UUID-like"):
        await grader.verdict(request(question="题目 11111111-1111-4111-8111-111111111111"))

    create.assert_not_awaited()
