"""Deterministic 180-case grading harness dataset generator."""

from __future__ import annotations

import json
from itertools import product
from pathlib import Path
from typing import Any

GRADES = (1, 2, 3)
QUESTION_TYPES = ("calculation", "fill_blank", "choice")
DIFFICULTIES = ("easy", "medium", "hard")


def _core_case(grade: int, question_type: str, difficulty: str, index: int) -> dict[str, Any]:
    difficulty_index = DIFFICULTIES.index(difficulty)
    base = grade * 20 + difficulty_index * 30 + index + 1
    correct = index not in (1, 4)
    if question_type == "calculation":
        left, right = base, grade + difficulty_index + 2
        reference = str(left + right)
        student = reference if correct else str(left + right + 1)
        question = f"{left}+{right}=?"
    elif question_type == "fill_blank":
        reference = str(base)
        variants = (f"{base}元", str(base), f"{base}.0", str(base), f"{base}个")
        student = variants[index] if correct else str(base + 2)
        question = f"（ ）+1={base + 1}"
    else:
        options = "ABCD"
        reference = options[(base + index) % 4]
        wrong = options[(options.index(reference) + 1) % 4]
        student = (f"选项（{reference.lower()}）" if index % 2 else reference) if correct else wrong
        question = "请选择正确选项：A.1 B.2 C.3 D.4"
    return {
        "id": f"core-g{grade}-{question_type}-{difficulty}-{index + 1:02d}",
        "category": "core",
        "grade": grade,
        "question_type": question_type,
        "difficulty": difficulty,
        "question": question,
        "reference_answer": reference,
        "student_answer": student,
        "expected_correct": correct,
        "hint_level": index % 4,
        "tags": [f"grade-{grade}", question_type, difficulty],
    }


_EDGE_TEMPLATES: tuple[tuple[str, str, str, str, str, bool], ...] = (
    ("blank", "fill_blank", "1+1=?", "2", "", False),
    ("spaces", "fill_blank", "1+1=?", "2", "   ", False),
    ("chinese", "fill_blank", "300+72=?", "372", "三百七十二", True),
    ("unit", "fill_blank", "多少钱？", "5", "5元", True),
    ("fraction-cn", "fill_blank", "填分数", "3/4", "四分之三", True),
    ("decimal-fraction", "calculation", "1÷2=?", "1/2", "0.5", True),
    ("fullwidth", "fill_blank", "填数", "12.5", "１２．５千克", True),
    ("choice-lower", "choice", "选择", "B", "b", True),
    ("choice-wrapper", "choice", "选择", "C", "选项（c）", True),
    ("wrong-unit", "fill_blank", "长度", "8", "9米", False),
    ("emoji", "fill_blank", "2+3=?", "5", "5🎉", True),
    ("divide-zero", "calculation", "无定义", "0", "1/0", False),
    ("injection", "calculation", "安全", "0", "__import__('os')", False),
    ("parentheses", "calculation", "计算", "20", "(2+3)*4", True),
    ("negative", "calculation", "3-5=?", "-2", "负二", True),
)


def generate_cases() -> list[dict[str, Any]]:
    """Create 135 matrix cases plus 45 boundary cases in stable order."""
    cases = [
        _core_case(grade, question_type, difficulty, index)
        for grade, question_type, difficulty in product(GRADES, QUESTION_TYPES, DIFFICULTIES)
        for index in range(5)
    ]
    for grade in GRADES:
        for name, question_type, question, reference, student, expected in _EDGE_TEMPLATES:
            cases.append(
                {
                    "id": f"edge-g{grade}-{name}",
                    "category": "edge",
                    "grade": grade,
                    "question_type": question_type,
                    "difficulty": "edge",
                    "question": question,
                    "reference_answer": reference,
                    "student_answer": student,
                    "expected_correct": expected,
                    "hint_level": 0,
                    "tags": [f"grade-{grade}", "boundary", name],
                }
            )
    if len(cases) != 180 or len({case["id"] for case in cases}) != 180:
        raise AssertionError("dataset contract violated")
    return cases


def write_dataset(path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in generate_cases())
    destination.write_text(content, encoding="utf-8")
    return destination
