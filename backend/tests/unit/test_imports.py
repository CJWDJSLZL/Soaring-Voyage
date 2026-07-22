from __future__ import annotations

import pytest
from app.imports import parse_student_import


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
