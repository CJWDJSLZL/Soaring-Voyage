from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree


@dataclass(frozen=True)
class StudentImportRow:
    row_number: int
    display_name: str
    username: str
    initial_password: str
    grade_level: int
    class_name: str


@dataclass(frozen=True)
class ProblemImportRow:
    row_number: int
    problem_text: str
    problem_type: str
    reference_answer: str
    grade_level: int
    difficulty: str
    solution_steps: list[str]
    common_errors: list[dict[str, str]]
    tags: list[str]


_COLUMN_ALIASES = {
    "display_name": {"姓名", "display_name", "name", "student_name"},
    "username": {"用户名", "用户", "username", "user_name"},
    "initial_password": {"初始密码", "密码", "initial_password", "password"},
    "grade_level": {"年级", "grade", "grade_level"},
    "class_name": {"班级名称", "班级", "class_name"},
}

_PROBLEM_COLUMN_ALIASES = {
    "problem_text": {"problem_text", "题目", "题目文本"},
    "problem_type": {"problem_type", "题型"},
    "reference_answer": {"reference_answer", "参考答案", "答案"},
    "grade_level": {"grade_level", "grade", "年级"},
    "difficulty": {"difficulty", "难度"},
    "solution_steps": {"solution_steps", "解题步骤"},
    "common_errors": {"common_errors", "常见错误"},
    "tags": {"tags", "标签"},
}


def parse_student_import(filename: str, content: bytes) -> list[StudentImportRow]:
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("file must be at most 10MB")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "csv"
    if suffix in {"csv", "txt"}:
        records = _records_from_delimited(content, delimiter=",")
    elif suffix == "tsv":
        records = _records_from_delimited(content, delimiter="\t")
    elif suffix == "xlsx":
        records = _records_from_xlsx(content)
    else:
        raise ValueError("file must be CSV, TSV, or XLSX")
    return _normalize_records(records)


def parse_problem_import(filename: str, content: bytes) -> list[ProblemImportRow]:
    records = _records_from_file(filename, content)
    return _normalize_problem_records(records)


def _records_from_file(filename: str, content: bytes) -> list[dict[str, str]]:
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("file must be at most 10MB")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "csv"
    if suffix in {"csv", "txt"}:
        return _records_from_delimited(content, delimiter=",")
    if suffix == "tsv":
        return _records_from_delimited(content, delimiter="\t")
    if suffix == "xlsx":
        return _records_from_xlsx(content)
    raise ValueError("file must be CSV, TSV, or XLSX")


def _records_from_delimited(content: bytes, *, delimiter: str) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [{str(key or "").strip(): str(value or "").strip() for key, value in row.items()} for row in reader]


def _records_from_xlsx(content: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        shared = _xlsx_shared_strings(workbook)
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml")
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ElementTree.fromstring(sheet_xml)  # noqa: S314 -- bounded XLSX worksheet XML, no entity expansion support.
    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", namespace):
        values: list[str] = []
        current_column = 1
        for cell in row.findall("x:c", namespace):
            cell_ref = str(cell.attrib.get("r", ""))
            column = _xlsx_column_number(cell_ref)
            while current_column < column:
                values.append("")
                current_column += 1
            values.append(_xlsx_cell_text(cell, shared, namespace).strip())
            current_column += 1
        rows.append(values)
    if not rows:
        return []
    headers = rows[0]
    return [
        {headers[index].strip(): values[index].strip() if index < len(values) else "" for index in range(len(headers))}
        for values in rows[1:]
    ]


def _xlsx_shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    try:
        raw = workbook.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ElementTree.fromstring(raw)  # noqa: S314 -- bounded XLSX shared strings XML, no entity expansion support.
    return [
        "".join(text.text or "" for text in item.findall(".//x:t", namespace))
        for item in root.findall("x:si", namespace)
    ]


def _xlsx_cell_text(cell: ElementTree.Element, shared: list[str], namespace: dict[str, str]) -> str:
    if cell.attrib.get("t") == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//x:t", namespace))
    value = cell.find("x:v", namespace)
    if value is None or value.text is None:
        return ""
    if cell.attrib.get("t") == "s":
        index = int(value.text)
        return shared[index] if 0 <= index < len(shared) else ""
    return value.text


def _xlsx_column_number(cell_ref: str) -> int:
    letters = "".join(char for char in cell_ref if char.isalpha())
    number = 0
    for char in letters:
        number = number * 26 + ord(char.upper()) - ord("A") + 1
    return max(number, 1)


def _normalize_records(records: list[dict[str, str]]) -> list[StudentImportRow]:
    rows: list[StudentImportRow] = []
    for offset, record in enumerate(records, start=2):
        mapped = {_canonical_header(header): value.strip() for header, value in record.items()}
        if not any(mapped.values()):
            continue
        missing = [field for field in _COLUMN_ALIASES if not mapped.get(field)]
        if missing:
            raise ValueError(f"row {offset}: missing required columns {', '.join(missing)}")
        try:
            grade_level = int(mapped["grade_level"])
        except ValueError as exc:
            raise ValueError(f"row {offset}: grade_level must be an integer") from exc
        if grade_level < 1 or grade_level > 6:
            raise ValueError(f"row {offset}: grade_level must be between 1 and 6")
        rows.append(
            StudentImportRow(
                row_number=offset,
                display_name=mapped["display_name"],
                username=mapped["username"],
                initial_password=mapped["initial_password"],
                grade_level=grade_level,
                class_name=mapped["class_name"],
            )
        )
    if not rows:
        raise ValueError("file contains no student rows")
    return rows


def _normalize_problem_records(records: list[dict[str, str]]) -> list[ProblemImportRow]:
    rows: list[ProblemImportRow] = []
    for offset, record in enumerate(records, start=2):
        mapped = {_canonical_problem_header(header): value.strip() for header, value in record.items()}
        if not any(mapped.values()):
            continue
        required = ("problem_text", "problem_type", "reference_answer", "grade_level", "difficulty")
        missing = [field for field in required if not mapped.get(field)]
        if missing:
            raise ValueError(f"row {offset}: missing required columns {', '.join(missing)}")
        try:
            grade_level = int(mapped["grade_level"])
        except ValueError as exc:
            raise ValueError(f"row {offset}: grade_level must be an integer") from exc
        if grade_level < 1 or grade_level > 6:
            raise ValueError(f"row {offset}: grade_level must be between 1 and 6")
        problem_type = mapped["problem_type"]
        if problem_type not in {"arithmetic", "fill_in_blank", "multiple_choice"}:
            raise ValueError(f"row {offset}: problem_type must be arithmetic, fill_in_blank, or multiple_choice")
        difficulty = mapped["difficulty"]
        if difficulty not in {"easy", "medium", "hard"}:
            raise ValueError(f"row {offset}: difficulty must be easy, medium, or hard")
        rows.append(
            ProblemImportRow(
                row_number=offset,
                problem_text=mapped["problem_text"],
                problem_type=problem_type,
                reference_answer=mapped["reference_answer"],
                grade_level=grade_level,
                difficulty=difficulty,
                solution_steps=_split_list(mapped.get("solution_steps", "")),
                common_errors=_parse_common_errors(mapped.get("common_errors", "")),
                tags=_split_list(mapped.get("tags", "")),
            )
        )
    if not rows:
        raise ValueError("file contains no problem rows")
    return rows


def _split_list(value: str) -> list[str]:
    if not value:
        return []
    separators = [";", "；", "|", "，", ","]
    values = [value]
    for separator in separators:
        if separator in value:
            values = value.split(separator)
            break
    return [item.strip() for item in values if item.strip()]


def _parse_common_errors(value: str) -> list[dict[str, str]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [{"note": item} for item in _split_list(value)]
    if not isinstance(parsed, list):
        raise ValueError("common_errors must be a JSON array")
    return [item for item in parsed if isinstance(item, dict)]


def _canonical_header(header: str) -> str:
    normalized = header.strip()
    for canonical, aliases in _COLUMN_ALIASES.items():
        if normalized in aliases:
            return canonical
    return normalized


def _canonical_problem_header(header: str) -> str:
    normalized = header.strip()
    for canonical, aliases in _PROBLEM_COLUMN_ALIASES.items():
        if normalized in aliases:
            return canonical
    return normalized
