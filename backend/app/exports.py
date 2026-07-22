from __future__ import annotations

import html
import io
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.domain.models import JsonDict

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def assignment_export_filename(assignment_id: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d")
    short_id = "".join(character for character in assignment_id if character.isalnum())[:8] or "assignment"
    return f"assignment_report_{short_id}_{timestamp}.xlsx"


def build_assignment_report_xlsx(report: JsonDict) -> bytes:
    problem_rows = [
        [
            "题号",
            "题目ID",
            "题目",
            "总尝试次数",
            "首次答对",
            "提示后答对",
            "仍错误",
            "待审核",
            "首次正确率",
            "平均提示次数",
            "错误类型分布",
        ]
    ]
    for item in report["problem_stats"]:
        problem_rows.append(
            [
                item["sequence"],
                item["problem_id"],
                item["problem_text"],
                item["total_attempts"],
                item["correct_first_try"],
                item["correct_after_hint"],
                item["still_wrong"],
                item["pending_review"],
                item["accuracy_first_try"],
                item["avg_hint_used"],
                _error_summary(item.get("top_error_types", [])),
            ]
        )

    student_rows = [
        [
            "学生ID",
            "学生姓名",
            "提交ID",
            "提交状态",
            "提交时间",
            "题号",
            "题目ID",
            "题目",
            "学生答案",
            "是否正确",
            "错误类型",
            "使用提示次数",
            "尝试次数",
            "置信度",
            "是否人工审核",
        ]
    ]
    for submission in report["student_rows"]:
        for result in submission["results"]:
            student_rows.append(
                [
                    submission["student_id"],
                    submission["student_name"],
                    submission["submission_id"],
                    submission["status"],
                    submission["submitted_at"],
                    result["sequence"],
                    result["problem_id"],
                    result["problem_text"],
                    result["student_answer"],
                    _correct_label(result["is_correct"]),
                    result.get("error_type") or "",
                    result["hint_level"],
                    result["attempt_number"],
                    result["confidence_score"],
                    "是" if result["routed_to_human"] else "否",
                ]
            )

    summary_rows = [["section", "name", "count", "rate", "affected_student_count", "alert_level", "alert"]]
    for error_type, count in sorted((report.get("error_distribution") or {}).items()):
        summary_rows.append(["error_distribution", error_type, count, "", "", "", ""])
    for item in report.get("knowledge_point_alerts") or []:
        summary_rows.append(
            [
                "knowledge_point_alert",
                item["knowledge_point"],
                "",
                item["error_rate"],
                item["affected_student_count"],
                item["alert_level"],
                item["alert"],
            ]
        )
    if len(summary_rows) == 1:
        summary_rows.append(["none", "", "", "", "", "", ""])

    sheets = [
        ("题目维度", problem_rows),
        ("学生维度", student_rows),
        ("summary_alerts", summary_rows),
    ]
    return _build_xlsx(sheets)


def build_problem_import_template_xlsx() -> bytes:
    return _build_xlsx(
        [
            (
                "problem_import_template",
                [
                    [
                        "problem_text",
                        "problem_type",
                        "reference_answer",
                        "grade_level",
                        "difficulty",
                        "solution_steps",
                        "common_errors",
                        "tags",
                    ],
                    [
                        "325 + 47 = ___",
                        "arithmetic",
                        "372",
                        "3",
                        "medium",
                        "个位相加并处理进位;再计算十位和百位",
                        "计算错误;进位错误",
                        "加法;进位",
                    ],
                ],
            )
        ]
    )


def _build_xlsx(sheets: list[tuple[str, list[list[Any]]]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(len(sheets)))
        archive.writestr("_rels/.rels", _root_rels())
        archive.writestr("xl/workbook.xml", _workbook([name for name, _rows in sheets]))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        archive.writestr("xl/styles.xml", _styles())
        for index, (_name, rows) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet(rows))
    return output.getvalue()


def _error_summary(items: Iterable[JsonDict]) -> str:
    return "; ".join(f"{item['error_type']}:{item['count']}" for item in items)


def _correct_label(value: Any) -> str:
    if value is True:
        return "正确"
    if value is False:
        return "错误"
    return "待审核"


def _content_types(sheet_count: int) -> str:
    sheet_overrides = "\n".join(
        f'  <Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{sheet_overrides}
</Types>"""


def _root_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _workbook(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{html.escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>{sheets}</sheets>
</workbook>"""


def _workbook_rels(sheet_count: int) -> str:
    relationships = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    relationships += f'<Relationship Id="rId{sheet_count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}</Relationships>"""


def _styles() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>"""


def _sheet(rows: list[list[Any]]) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            cell_ref = f"{_column_name(column_index)}{row_index}"
            style = ' s="1"' if row_index == 1 else ""
            cells.append(f'<c r="{cell_ref}" t="inlineStr"{style}><is><t>{_cell_text(value)}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    dimension = f"A1:{_column_name(max((len(row) for row in rows), default=1))}{max(len(rows), 1)}"
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="{dimension}"/>
  <sheetData>{"".join(row_xml)}</sheetData>
</worksheet>"""


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return html.escape(text, quote=False)


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name
