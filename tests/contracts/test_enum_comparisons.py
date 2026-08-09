"""Тесты TASK-015: enum-устойчивость validate_review ([REQ-015]).

`model_copy(update={"verdict": "approve"})` кладёт в поле строку без
ревалидации — `is`-сравнение с членом enum молча пропускает правило
REQ-010 ([DESIGN-013]). Защита двухслойная: ревалидация входа конвейера
`validate_review` и `==`-сравнения в `check_*` (StrEnum сравнивает и
члены, и строки). Схемно-невалидный вход даёт `ValidationError`. Новый
файл — тест-файлы итераций 1-2 байт-неизменны (NFR-003). Все тесты — на
голых моделях, без диска и моков ([DESIGN-008]: функции без I/O).
"""

from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.review import Review
from disputatio.contracts.validation import (
    REASON_APPROVE_ON_FAILED_GATES,
    REASON_NO_SUBSTANTIVE_ISSUES,
    check_verdict_vs_verification,
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
    """Review из issue-payload'ов — вход validate_review и check_*-функций."""
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


def test_string_verdict_approve_on_failed_gates_rejected() -> None:
    """REQ-015→REQ-010: строковый verdict "approve" при fail → код причины.

    `model_copy(update={"verdict": "approve"})` кладёт строку вместо
    `Verdict.APPROVE`; конвейер обязан всё равно отклонить approve при
    `overall == fail`.
    """
    review = make_review([]).model_copy(update={"verdict": "approve"})
    acceptance = validate_review(review, make_verification("fail"))
    assert acceptance.accepted is False
    assert acceptance.rejection_reasons == [REASON_APPROVE_ON_FAILED_GATES]


def test_string_verdict_request_changes_without_issues_rejected() -> None:
    """REQ-015→REQ-008: строковый "request_changes" без issues → код причины."""
    review = make_review([], verdict="approve").model_copy(
        update={"verdict": "request_changes"}
    )
    acceptance = validate_review(review, make_verification("pass"))
    assert acceptance.accepted is False
    assert acceptance.rejection_reasons == [REASON_NO_SUBSTANTIVE_ISSUES]


def test_string_overall_fail_triggers_rule() -> None:
    """REQ-015→REQ-010: строковый overall "fail" у VerificationReport ловится."""
    review = make_review([], verdict="approve")
    verification = make_verification("pass").model_copy(update={"overall": "fail"})
    acceptance = validate_review(review, verification)
    assert acceptance.accepted is False
    assert acceptance.rejection_reasons == [REASON_APPROVE_ON_FAILED_GATES]


def test_check_verdict_vs_verification_string_values_match_enum() -> None:
    """REQ-015: прямой вызов check_* со строками — идентично enum-членам."""
    enum_review = make_review([], verdict="approve")
    enum_verification = make_verification("fail")
    str_review = make_review([]).model_copy(update={"verdict": "approve"})
    str_verification = make_verification("pass").model_copy(update={"overall": "fail"})
    expected = check_verdict_vs_verification(enum_review, enum_verification)
    assert expected == REASON_APPROVE_ON_FAILED_GATES
    assert check_verdict_vs_verification(str_review, str_verification) == expected
    passing = make_verification("fail").model_copy(update={"overall": "pass"})
    assert check_verdict_vs_verification(str_review, passing) is None


def test_invalid_verdict_string_raises_validation_error() -> None:
    """REQ-015: заведомо невалидный verdict "yolo" → ValidationError.

    Ревалидация входа конвейера отклоняет схемно-невалидную модель;
    ошибка обрабатывается слоем schema-retry оркестратора.
    """
    review = make_review([]).model_copy(update={"verdict": "yolo"})
    with pytest.raises(ValidationError):
        validate_review(review, make_verification("pass"))
