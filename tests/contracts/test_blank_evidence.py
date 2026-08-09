"""Тесты TASK-013: whitespace-hardening evidence и checked ([REQ-013]).

Закрывают whitespace-обход анти-галлюцинационного ядра §4.4
([DESIGN-011]): evidence из одних пробельных символов эквивалентен
пустому (REQ-009), `checked` без единого содержательного элемента
эквивалентен пустому (REQ-011). Новый файл — тест-файлы итерации 1
байт-неизменны (NFR-003). Все тесты — на голых моделях, без диска и
моков ([DESIGN-008]: функции без I/O).
"""

from typing import Any

from disputatio.contracts.review import Review
from disputatio.contracts.validation import (
    REASON_EMPTY_CHECKED,
    REASON_NO_SUBSTANTIVE_ISSUES,
    check_checked_nonempty,
    degrade_unevidenced_issues,
    validate_review,
)
from disputatio.contracts.verification import VerificationReport


def make_issue(
    issue_id: str, severity: str, evidence: str = "подтверждено: цитата diff"
) -> dict[str, Any]:
    """Payload одного issue; `evidence` непустой по умолчанию."""
    return {
        "id": issue_id,
        "severity": severity,
        "file": "src/x.py",
        "claim": "что не так, проверяемая формулировка",
        "evidence": evidence,
    }


def make_review(
    issues: list[dict[str, Any]],
    verdict: str = "request_changes",
    checked: list[str] | None = None,
) -> Review:
    """Review из issue-payload'ов — вход degrade_* и check_*-функций."""
    return Review.model_validate(
        {
            "schema": "disputatio/v1",
            "round": 3,
            "role": "reviewer",
            "verdict": verdict,
            "confidence": 0.8,
            "issues": issues,
            "checked": ["прочитал diff"] if checked is None else checked,
            "summary": "1-3 предложения",
        }
    )


def make_verification(overall: str) -> VerificationReport:
    """VerificationReport с заданным overall — вход validate_review."""
    return VerificationReport.model_validate(
        {
            "schema": "disputatio/v1",
            "round": 3,
            "gates": [],
            "overall": overall,
            "diff_stats": {"files": 1, "insertions": 2, "deletions": 0},
        }
    )


def test_blocker_with_spaces_only_evidence_degraded_to_minor() -> None:
    """REQ-013: blocker с evidence из пробелов → minor, id в degraded_ids."""
    review = make_review([make_issue("R3-1", "blocker", evidence="   ")])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "minor"
    assert degraded_ids == ["R3-1"]


def test_major_with_tab_newline_evidence_degraded_to_minor() -> None:
    """REQ-013: major с evidence == "\\t\\n" деградируется как при пустом."""
    review = make_review([make_issue("R3-1", "major", evidence="\t\n")])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "minor"
    assert degraded_ids == ["R3-1"]


def test_whitespace_only_checked_rejected() -> None:
    """REQ-013: непустой checked без содержательных элементов → код причины.

    `checked == ["   ", "\\t\\n"]` даёт тот же `REASON_EMPTY_CHECKED`,
    что и `checked == []` — новый код причины не вводится.
    """
    review = make_review([], verdict="approve", checked=["   ", "\t\n"])
    assert check_checked_nonempty(review) == REASON_EMPTY_CHECKED


def test_checked_with_substantive_item_among_blanks_passes() -> None:
    """REQ-013: один содержательный элемент среди пробельных — проходит."""
    review = make_review(
        [], verdict="approve", checked=["   ", "прочитал diff", "\t\n"]
    )
    assert check_checked_nonempty(review) is None


def test_whitespace_evidence_degradation_rejects_review_end_to_end() -> None:
    """REQ-013→REQ-008: whitespace-evidence снял последний blocker → отказ.

    Сквозной прогон через `validate_review`: request_changes с одним
    blocker, чей evidence — пробельная строка; деградация оставляет
    ревью без blocker|major, и конвейер отклоняет его с
    `REASON_NO_SUBSTANTIVE_ISSUES`.
    """
    review = make_review([make_issue("R3-1", "blocker", evidence="   ")])
    acceptance = validate_review(review, make_verification("pass"))
    assert acceptance.accepted is False
    assert acceptance.rejection_reasons == [REASON_NO_SUBSTANTIVE_ISSUES]
    assert acceptance.degraded_issue_ids == ["R3-1"]
    assert acceptance.review.issues[0].severity == "minor"
