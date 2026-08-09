"""Тесты TASK-001 ревизии 3: zero-width-hardening evidence и checked.

Закрывают Cf-обход анти-галлюцинационного ядра §4.4 ([REQ-001],
[REQ-002], [DESIGN-001], [DESIGN-002]): `str.strip()` не считает
символы Unicode-категории Cf (U+200B ZERO WIDTH SPACE, U+FEFF BOM)
пробельными, поэтому критерий `not text.strip()` обходится невидимыми
символами. Матрица «оба символа × оба места»: evidence
(`degrade_unevidenced_issues`) и checked (`check_checked_nonempty`),
standalone и сквозь `validate_review`. Cf-символы в тестах — только
escape-последовательностями (`"\\u200b"`, `"\\ufeff"`), без литеральных
невидимых символов в исходнике. Новый файл — существующие тест-файлы
байт-неизменны ([REQ-003], [REQ-008]). Все тесты — на голых моделях,
без диска и моков ([DESIGN-008] ревизии 1: функции без I/O).
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


def test_blocker_with_zero_width_space_evidence_degraded_to_minor() -> None:
    """REQ-001: blocker с evidence == "\\u200b" → minor, id в degraded_ids."""
    review = make_review([make_issue("R3-1", "blocker", evidence="\u200b")])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "minor"
    assert degraded_ids == ["R3-1"]


def test_major_with_bom_evidence_degraded_to_minor() -> None:
    """REQ-001: major с evidence == "\\ufeff" деградируется как при пустом."""
    review = make_review([make_issue("R3-1", "major", evidence="\ufeff")])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "minor"
    assert degraded_ids == ["R3-1"]


def test_mixed_cf_and_whitespace_evidence_degraded() -> None:
    """REQ-001: смесь Cf и обычных пробельных (" \\u200b\\t\\ufeff ") → minor."""
    review = make_review([make_issue("R3-1", "blocker", evidence=" \u200b\t\ufeff ")])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "minor"
    assert degraded_ids == ["R3-1"]


def test_checked_zero_width_space_only_rejected() -> None:
    """REQ-002: checked == ["\\u200b"] → REASON_EMPTY_CHECKED."""
    review = make_review([], verdict="approve", checked=["\u200b"])
    assert check_checked_nonempty(review) == REASON_EMPTY_CHECKED


def test_checked_bom_only_rejected() -> None:
    """REQ-002: checked == ["\\ufeff"] → REASON_EMPTY_CHECKED."""
    review = make_review([], verdict="approve", checked=["\ufeff"])
    assert check_checked_nonempty(review) == REASON_EMPTY_CHECKED


def test_checked_mixed_cf_whitespace_items_rejected() -> None:
    """REQ-002: checked == [" \\u200b ", "\\ufeff"] → REASON_EMPTY_CHECKED."""
    review = make_review([], verdict="approve", checked=[" \u200b ", "\ufeff"])
    assert check_checked_nonempty(review) == REASON_EMPTY_CHECKED


def test_cf_evidence_and_checked_rejected_end_to_end() -> None:
    """REQ-001→REQ-002: Cf-обход отклоняется validate_review, причины — ВСЕ.

    request_changes с единственным blocker (evidence — U+200B) и checked
    из одного U+FEFF: деградация снимает последний blocker|major →
    негативный вердикт не обоснован, и оба кода причин накапливаются.
    """
    review = make_review(
        [make_issue("R3-1", "blocker", evidence="\u200b")],
        checked=["\ufeff"],
    )
    acceptance = validate_review(review, make_verification("pass"))
    assert acceptance.accepted is False
    assert acceptance.rejection_reasons == [
        REASON_NO_SUBSTANTIVE_ISSUES,
        REASON_EMPTY_CHECKED,
    ]
    assert acceptance.degraded_issue_ids == ["R3-1"]
    assert acceptance.review.issues[0].severity == "minor"


def test_bom_evidence_degradation_rejects_review_end_to_end() -> None:
    """REQ-001: major с evidence == "\\ufeff" — сквозной отказ validate_review."""
    review = make_review([make_issue("R3-2", "major", evidence="\ufeff")])
    acceptance = validate_review(review, make_verification("pass"))
    assert acceptance.accepted is False
    assert acceptance.rejection_reasons == [REASON_NO_SUBSTANTIVE_ISSUES]
    assert acceptance.degraded_issue_ids == ["R3-2"]


def test_substantive_evidence_with_cf_prefix_not_degraded() -> None:
    """Позитивный контроль REQ-001: "\\u200bреальный факт" — деградации нет."""
    review = make_review(
        [make_issue("R3-1", "blocker", evidence="\u200bреальный факт")]
    )
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "blocker"
    assert degraded_ids == []


def test_substantive_checked_item_with_cf_prefix_passes() -> None:
    """Позитивный контроль REQ-002: содержательный элемент среди Cf проходит."""
    review = make_review(
        [], verdict="approve", checked=["\u200b", "\u200bреальный факт"]
    )
    assert check_checked_nonempty(review) is None


def test_input_not_mutated_and_evidence_preserved_in_copy() -> None:
    """Инвариант DESIGN-001: вход не мутируется, evidence хранится как пришёл.

    Нормализация — только для критерия: у исходного review severity
    остаётся blocker, а в деградированной копии меняется лишь severity,
    текст evidence сохранён байт-в-байт (U+200B не вырезан).
    """
    review = make_review([make_issue("R3-1", "blocker", evidence="\u200b")])
    degraded, _ = degrade_unevidenced_issues(review)
    assert review.issues[0].severity == "blocker"
    assert review.issues[0].evidence == "\u200b"
    assert degraded.issues[0].evidence == "\u200b"
