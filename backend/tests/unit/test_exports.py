from __future__ import annotations

import io
import zipfile

from app.exports import build_assignment_report_xlsx


def test_assignment_report_xlsx_includes_summary_alert_sheet() -> None:
    workbook_bytes = build_assignment_report_xlsx(
        {
            "problem_stats": [
                {
                    "sequence": 1,
                    "problem_id": "problem-1",
                    "problem_text": "1 + 1 = ___",
                    "total_attempts": 2,
                    "correct_first_try": 1,
                    "correct_after_hint": 0,
                    "still_wrong": 1,
                    "pending_review": 0,
                    "accuracy_first_try": 0.5,
                    "avg_hint_used": 0.5,
                    "top_error_types": [{"error_type": "calculation_error", "count": 1}],
                }
            ],
            "student_rows": [
                {
                    "student_id": "student-1",
                    "student_name": "Student",
                    "submission_id": "submission-1",
                    "status": "graded",
                    "submitted_at": "2026-07-22T08:00:00+00:00",
                    "results": [
                        {
                            "sequence": 1,
                            "problem_id": "problem-1",
                            "problem_text": "1 + 1 = ___",
                            "student_answer": "3",
                            "is_correct": False,
                            "error_type": "calculation_error",
                            "hint_level": 1,
                            "attempt_number": 2,
                            "confidence_score": 0.9,
                            "routed_to_human": False,
                        }
                    ],
                }
            ],
            "error_distribution": {"calculation_error": 1},
            "knowledge_point_alerts": [
                {
                    "knowledge_point": "addition",
                    "error_rate": 0.5,
                    "alert_level": "high",
                    "alert": "review addition",
                    "affected_student_count": 1,
                }
            ],
        }
    )

    with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as workbook:
        workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        summary_sheet = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")

    assert 'name="summary_alerts"' in workbook_xml
    assert "error_distribution" in summary_sheet
    assert "calculation_error" in summary_sheet
    assert "knowledge_point_alert" in summary_sheet
    assert "addition" in summary_sheet
