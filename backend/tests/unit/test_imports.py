from __future__ import annotations

import pytest
from app.imports import parse_problem_import, parse_student_import


def test_parse_student_import_accepts_chinese_csv_headers() -> None:
    content = "姓名,用户名,初始密码,年级,班级名称\n张三,zhangsan,Pass1234,3,三年级A班\n".encode()

    rows = parse_student_import("students.csv", content)

    assert len(rows) == 1
    assert rows[0].row_number == 2
    assert rows[0].display_name == "张三"
    assert rows[0].username == "zhangsan"
    assert rows[0].initial_password == "Pass1234"
    assert rows[0].grade_level == 3
    assert rows[0].class_name == "三年级A班"


def test_parse_student_import_rejects_missing_required_data() -> None:
    content = "姓名,用户名,年级,班级名称\n张三,zhangsan,3,三年级A班\n".encode()

    with pytest.raises(ValueError, match="missing required columns"):
        parse_student_import("students.csv", content)


def test_parse_problem_import_accepts_csv_rows() -> None:
    content = (
        "problem_text,problem_type,reference_answer,grade_level,difficulty,solution_steps,tags\n"
        "1 + 1 = ___,arithmetic,2,1,easy,先算个位;写出结果,加法;一年级\n"
    ).encode()

    rows = parse_problem_import("problems.csv", content)

    assert len(rows) == 1
    assert rows[0].problem_text == "1 + 1 = ___"
    assert rows[0].problem_type == "arithmetic"
    assert rows[0].reference_answer == "2"
    assert rows[0].grade_level == 1
    assert rows[0].difficulty == "easy"
    assert rows[0].solution_steps == ["先算个位", "写出结果"]
    assert rows[0].tags == ["加法", "一年级"]


def test_parse_problem_import_rejects_unknown_problem_type() -> None:
    content = "problem_text,problem_type,reference_answer,grade_level,difficulty\n题目,essay,2,1,easy\n".encode()

    with pytest.raises(ValueError, match="problem_type"):
        parse_problem_import("problems.csv", content)
