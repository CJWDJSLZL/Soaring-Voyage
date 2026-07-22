from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^\S+$")
    password: str = Field(min_length=6, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=6, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class TicketRequest(BaseModel):
    submission_id: str = Field(min_length=1)


class ProblemCreate(BaseModel):
    problem_text: str = Field(min_length=1, max_length=500)
    problem_type: Literal["arithmetic", "fill_in_blank", "multiple_choice"]
    reference_answer: str = Field(min_length=1, max_length=200)
    grade_level: int = Field(ge=1, le=6)
    difficulty: Literal["easy", "medium", "hard"]
    curriculum_version: Literal["人教版", "北师大版"]
    solution_steps: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=20)
    common_errors: list[dict] = Field(default_factory=list, max_length=10)
    tags: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(default_factory=list, max_length=20)


class AssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    class_ids: list[str] = Field(min_length=1, max_length=20)
    due_date: datetime | None = None
    problem_ids: list[str] = Field(min_length=1, max_length=50)


class AssignmentPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    due_date: datetime | None = None
    class_ids: list[str] | None = Field(None, min_length=1, max_length=20)
    add_problem_ids: list[str] = Field(default_factory=list, max_length=50)
    remove_problem_ids: list[str] = Field(default_factory=list, max_length=50)


class ClassCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    grade_level: int = Field(ge=1, le=6)
    teacher_id: str = Field(min_length=1)
    academic_year: str = Field(min_length=4, max_length=20)


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=6, max_length=128)


class AdminUserStatusRequest(BaseModel):
    is_active: bool


class HarnessRunRequest(BaseModel):
    use_mock: bool = True
    sample_rate: float = Field(default=1.0, ge=0.01, le=1.0)
    dataset: str = Field(default="all", max_length=100)
    grade_levels: list[int] = Field(default_factory=list, max_length=6)

    @field_validator("grade_levels")
    @classmethod
    def validate_grade_levels(cls, value: list[int]) -> list[int]:
        if any(level < 1 or level > 6 for level in value):
            raise ValueError("grade levels must be between 1 and 6")
        if len(value) != len(set(value)):
            raise ValueError("grade levels must be unique")
        return value


class RagIngestRequest(BaseModel):
    source: Literal["problems_table"] = "problems_table"
    grade_levels: list[int] = Field(default_factory=list, max_length=6)
    batch_size: int = Field(default=100, ge=1, le=1000)
    force_reingest: bool = False

    @field_validator("grade_levels")
    @classmethod
    def validate_grade_levels(cls, value: list[int]) -> list[int]:
        if any(level < 1 or level > 6 for level in value):
            raise ValueError("grade levels must be between 1 and 6")
        if len(value) != len(set(value)):
            raise ValueError("grade levels must be unique")
        return value


class Answer(BaseModel):
    problem_id: str = Field(min_length=1)
    answer_text: str = Field(max_length=500)

    @field_validator("answer_text")
    @classmethod
    def trim_answer(cls, value: str) -> str:
        return value.strip()


class SubmissionCreate(BaseModel):
    assignment_id: str = Field(min_length=1)
    answers: list[Answer] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_problems(self):
        ids = [answer.problem_id for answer in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("problem_id must be unique")
        return self


class HintRequest(BaseModel):
    problem_id: str = Field(min_length=1)
    new_answer: str = Field(max_length=500)


class ReviewRequest(BaseModel):
    override_correct: bool
    override_error_type: Literal["计算错误", "审题错误", "进位错误", "概念错误"] | None = None
    override_feedback: str | None = Field(None, max_length=1000)
    reviewer_notes: str | None = Field(None, max_length=500)
    is_training_example: bool = False

    @model_validator(mode="after")
    def error_type_required(self):
        if not self.override_correct and self.override_error_type is None:
            raise ValueError("override_error_type is required when answer is incorrect")
        return self
