"""Тесты validation.py — degrade_unevidenced_issues: TASK-008, [REQ-009].

Импорты `disputatio.contracts.validation` выполняются внутри тестов: на
момент red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы
collection. Red-селектор (`test_blocker_without_evidence_degraded_to_minor`)
превращает ImportError в AssertionError — гейт принимает red только при
падении assertion'ом. Все тесты — на голых моделях, без диска и моков
([DESIGN-008]: функция без I/O).
"""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.review import Review


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
    issues: list[dict[str, Any]], verdict: str = "request_changes"
) -> Review:
    """Review из issue-payload'ов — вход degrade_unevidenced_issues."""
    return Review.model_validate(
        {
            "schema": "disputatio/v1",
            "round": 3,
            "role": "reviewer",
            "verdict": verdict,
            "confidence": 0.8,
            "issues": issues,
            "checked": ["прочитал diff"],
            "summary": "1-3 предложения",
        }
    )


def test_blocker_without_evidence_degraded_to_minor() -> None:
    """Blocker с пустым evidence → в новом Review minor, id в degraded_ids."""
    try:
        from disputatio.contracts.validation import degrade_unevidenced_issues
    except ImportError as exc:  # red-фаза: validation.py ещё не создан
        raise AssertionError(
            "src/disputatio/contracts/validation.py ещё не создан"
        ) from exc

    review = make_review([make_issue("R3-1", "blocker", evidence="")])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "minor"
    assert degraded_ids == ["R3-1"]


def test_major_without_evidence_degraded_to_minor() -> None:
    """Major с пустым evidence деградируется так же, как blocker."""
    from disputatio.contracts.validation import degrade_unevidenced_issues

    review = make_review([make_issue("R3-1", "major", evidence="")])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "minor"
    assert degraded_ids == ["R3-1"]


def test_missing_evidence_field_degraded() -> None:
    """Отсутствующий `evidence` (дефолт "") — тоже «без evidence» (REQ-009)."""
    from disputatio.contracts.validation import degrade_unevidenced_issues

    payload = make_issue("R3-1", "blocker")
    del payload["evidence"]
    review = make_review([payload])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded.issues[0].severity == "minor"
    assert degraded_ids == ["R3-1"]


def test_original_review_not_mutated() -> None:
    """Исходный `review` не мутирован; результат — новый объект."""
    from disputatio.contracts.validation import degrade_unevidenced_issues

    review = make_review(
        [
            make_issue("R3-1", "blocker", evidence=""),
            make_issue("R3-2", "major"),
        ]
    )
    snapshot = copy.deepcopy(review)
    degraded, _ = degrade_unevidenced_issues(review)
    assert review == snapshot
    assert degraded is not review
    assert review.issues[0].severity == "blocker"


def test_evidenced_and_low_severity_issues_untouched() -> None:
    """Issues с evidence и minor/nit без evidence не затрагиваются."""
    from disputatio.contracts.validation import degrade_unevidenced_issues

    review = make_review(
        [
            make_issue("R3-1", "blocker"),
            make_issue("R3-2", "major"),
            make_issue("R3-3", "minor", evidence=""),
            make_issue("R3-4", "nit", evidence=""),
        ]
    )
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded_ids == []
    assert degraded.issues == review.issues
    assert degraded == review


def test_mixed_issues_only_unevidenced_degraded_order_kept() -> None:
    """Деградируются только голословные blocker|major; порядок сохранён."""
    from disputatio.contracts.validation import degrade_unevidenced_issues

    review = make_review(
        [
            make_issue("R3-1", "blocker", evidence=""),
            make_issue("R3-2", "blocker"),
            make_issue("R3-3", "major", evidence=""),
        ]
    )
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert [issue.id for issue in degraded.issues] == ["R3-1", "R3-2", "R3-3"]
    assert [issue.severity for issue in degraded.issues] == [
        "minor",
        "blocker",
        "minor",
    ]
    assert degraded_ids == ["R3-1", "R3-3"]


def test_verdict_and_other_fields_unchanged() -> None:
    """Деградация не меняет verdict и остальные поля Review (REQ-009)."""
    from disputatio.contracts.validation import degrade_unevidenced_issues

    review = make_review([make_issue("R3-1", "blocker", evidence="")], verdict="reject")
    degraded, _ = degrade_unevidenced_issues(review)
    assert degraded.verdict == "reject"
    assert degraded.round == review.round
    assert degraded.confidence == review.confidence
    assert degraded.checked == review.checked
    assert degraded.summary == review.summary


def test_no_issues_nothing_degraded() -> None:
    """Пустой `issues` — деградировать нечего, degraded_ids пуст."""
    from disputatio.contracts.validation import degrade_unevidenced_issues

    review = make_review([])
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded == review
    assert degraded_ids == []


def test_review_acceptance_defaults() -> None:
    """ReviewAcceptance: списки по умолчанию пусты, review хранится как есть."""
    from disputatio.contracts.validation import ReviewAcceptance

    review = make_review([])
    acceptance = ReviewAcceptance(accepted=True, review=review)
    assert acceptance.accepted is True
    assert acceptance.review == review
    assert acceptance.degraded_issue_ids == []
    assert acceptance.rejection_reasons == []


def test_review_acceptance_frozen_and_extra_forbidden() -> None:
    """ReviewAcceptance — frozen, лишние ключи отклоняются (ArtifactChild)."""
    from disputatio.contracts.validation import ReviewAcceptance

    review = make_review([])
    acceptance = ReviewAcceptance(accepted=False, review=review)
    with pytest.raises(ValidationError):
        acceptance.accepted = True
    with pytest.raises(ValidationError):
        ReviewAcceptance(accepted=True, review=review, extra_key="x")
