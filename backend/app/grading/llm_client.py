"""OpenAI-compatible LLM client for grading verdicts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any

from .engine import GradeRequest, LLMVerdict

if TYPE_CHECKING:
    from app.config import Settings

_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

SYSTEM_PROMPT = """你是小学数学批改助手。只判断学生答案是否等价于参考答案，并返回严格 JSON。
规则：
1. 不要输出 JSON 以外的文字。
2. 不要要求或推断学生姓名、学号、用户 ID、班级 ID。
3. 计算题若涉及纯数值运算，给出谨慎置信度；最终系统会用 SymPy 复核。
4. confidence 取 0 到 1。答案含糊、题意不足或格式异常时降低置信度。

JSON schema:
{"is_correct": boolean, "confidence": number, "feedback": string}
"""


class LLMUnavailableError(RuntimeError):
    """Raised when all configured LLM attempts fail."""


@dataclass(frozen=True)
class LLMClientConfig:
    api_key: str | None
    base_url: str
    primary_model: str
    fallback_model: str
    max_retries: int
    timeout_seconds: float
    use_mock: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMClientConfig:
        return cls(
            api_key=settings.deepseek_api_key,
            base_url=settings.llm_base_url,
            primary_model=settings.llm_primary_model,
            fallback_model=settings.llm_fallback_model,
            max_retries=settings.max_llm_retries,
            timeout_seconds=settings.llm_timeout_seconds,
            use_mock=settings.use_mock_llm,
        )


class DeepSeekGradingClient:
    def __init__(self, config: LLMClientConfig, client: Any | None = None) -> None:
        self.config = config
        self._client = client
        if self._client is None and self.is_enabled:
            openai = import_module("openai")
            self._client = openai.AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout_seconds,
                max_retries=0,
            )

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.api_key) and not self.config.use_mock

    @property
    def health_status(self) -> str:
        if self.config.use_mock:
            return "mock"
        return "configured" if self.is_enabled else "unconfigured"

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def verdict(self, request: GradeRequest) -> LLMVerdict | None:
        if not self.is_enabled or not request.student_answer.strip():
            return None
        openai = import_module("openai")
        retryable_errors = (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.APIError,
            ValueError,
        )
        payload = self._safe_payload(request)
        models = [self.config.primary_model]
        if self.config.fallback_model and self.config.fallback_model != self.config.primary_model:
            models.append(self.config.fallback_model)
        last_error: Exception | None = None
        for model in models:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    return await self._call_model(model, payload, attempt)
                except retryable_errors as exc:
                    last_error = exc
                    continue
        if last_error is not None:
            raise LLMUnavailableError("LLM grading unavailable") from last_error
        return None

    def _safe_payload(self, request: GradeRequest) -> dict[str, Any]:
        payload = {
            "question": request.question,
            "reference_answer": request.reference_answer,
            "student_answer": request.student_answer,
            "question_type": request.question_type,
            "grade": request.grade,
            "hint_level": request.hint_level,
            "solution_steps": request.solution_steps[:5],
        }
        rendered = json.dumps(payload, ensure_ascii=False)
        if _UUID_RE.search(rendered):
            raise ValueError("LLM payload contains UUID-like identifier")
        return payload

    async def _call_model(self, model: str, payload: dict[str, Any], attempt: int) -> LLMVerdict:
        if self._client is None:
            raise LLMUnavailableError("LLM client is not initialized")
        response = await self._client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "grade_math_answer",
                            "attempt": attempt,
                            "input": payload,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        content = response.choices[0].message.content or ""
        data = json.loads(content)
        return LLMVerdict(
            is_correct=bool(data["is_correct"]),
            confidence=float(data["confidence"]),
            feedback=str(data["feedback"])[:300] if data.get("feedback") else None,
        )
