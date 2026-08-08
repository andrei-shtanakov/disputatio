"""Тесты validation.py: TASK-008 (degrade, [REQ-009]) и TASK-009 (check_*).

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
    """VerificationReport с заданным overall — вход check_verdict_vs_verification."""
    return VerificationReport.model_validate(
        {
            "schema": "disputatio/v1",
            "round": 3,
            "gates": [],
            "overall": overall,
            "diff_stats": {"files": 1, "insertions": 2, "deletions": 0},
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


# --- TASK-009: check_* и константы причин ([REQ-008], [REQ-010], [REQ-011]) ---


def test_request_changes_without_issues_rejected() -> None:
    """REQ-008: request_changes с пустыми issues → REASON_NO_SUBSTANTIVE_ISSUES."""
    try:
        from disputatio.contracts.validation import (
            REASON_NO_SUBSTANTIVE_ISSUES,
            check_substantive_issues,
        )
    except ImportError as exc:  # red-фаза: check_* ещё не реализованы
        raise AssertionError("check_substantive_issues ещё не реализована") from exc

    review = make_review([], verdict="request_changes")
    assert check_substantive_issues(review) == REASON_NO_SUBSTANTIVE_ISSUES


def test_reject_without_blocker_or_major_rejected() -> None:
    """REQ-008: reject только с minor|nit issues → код причины."""
    from disputatio.contracts.validation import (
        REASON_NO_SUBSTANTIVE_ISSUES,
        check_substantive_issues,
    )

    review = make_review(
        [make_issue("R3-1", "minor"), make_issue("R3-2", "nit")],
        verdict="reject",
    )
    assert check_substantive_issues(review) == REASON_NO_SUBSTANTIVE_ISSUES


def test_negative_verdict_with_substantive_issue_passes() -> None:
    """REQ-008: ≥1 blocker|major — request_changes и reject проходят правило."""
    from disputatio.contracts.validation import check_substantive_issues

    with_blocker = make_review(
        [make_issue("R3-1", "minor"), make_issue("R3-2", "blocker")],
        verdict="request_changes",
    )
    with_major = make_review([make_issue("R3-1", "major")], verdict="reject")
    assert check_substantive_issues(with_blocker) is None
    assert check_substantive_issues(with_major) is None


def test_approve_not_touched_by_substantive_rule() -> None:
    """REQ-008: approve без issues правило не трогает → None."""
    from disputatio.contracts.validation import check_substantive_issues

    review = make_review([], verdict="approve")
    assert check_substantive_issues(review) is None


def test_approve_on_failed_gates_rejected() -> None:
    """REQ-010: approve при overall == fail → REASON_APPROVE_ON_FAILED_GATES."""
    from disputatio.contracts.validation import (
        REASON_APPROVE_ON_FAILED_GATES,
        check_verdict_vs_verification,
    )

    review = make_review([], verdict="approve")
    verification = make_verification("fail")
    assert (
        check_verdict_vs_verification(review, verification)
        == REASON_APPROVE_ON_FAILED_GATES
    )


def test_approve_on_passed_gates_passes() -> None:
    """REQ-010: approve при overall == pass проходит правило → None."""
    from disputatio.contracts.validation import check_verdict_vs_verification

    review = make_review([], verdict="approve")
    assert check_verdict_vs_verification(review, make_verification("pass")) is None


def test_request_changes_on_failed_gates_passes() -> None:
    """REQ-010: правило касается только approve — request_changes при fail → None."""
    from disputatio.contracts.validation import check_verdict_vs_verification

    review = make_review([make_issue("R3-1", "blocker")], verdict="request_changes")
    assert check_verdict_vs_verification(review, make_verification("fail")) is None


def test_empty_checked_rejected() -> None:
    """REQ-011: checked == [] → REASON_EMPTY_CHECKED."""
    from disputatio.contracts.validation import (
        REASON_EMPTY_CHECKED,
        check_checked_nonempty,
    )

    review = make_review([], verdict="approve", checked=[])
    assert check_checked_nonempty(review) == REASON_EMPTY_CHECKED


def test_nonempty_checked_passes() -> None:
    """REQ-011: непустой checked проходит правило → None."""
    from disputatio.contracts.validation import check_checked_nonempty

    review = make_review([], verdict="approve", checked=["прочитал diff"])
    assert check_checked_nonempty(review) is None


def test_checks_do_not_mutate_inputs() -> None:
    """Ни одна check_* не мутирует входные модели ([DESIGN-008]: без I/O)."""
    from disputatio.contracts.validation import (
        check_checked_nonempty,
        check_substantive_issues,
        check_verdict_vs_verification,
    )

    review = make_review([make_issue("R3-1", "minor")], verdict="reject")
    verification = make_verification("fail")
    review_snapshot = copy.deepcopy(review)
    verification_snapshot = copy.deepcopy(verification)
    check_substantive_issues(review)
    check_verdict_vs_verification(review, verification)
    check_checked_nonempty(review)
    assert review == review_snapshot
    assert verification == verification_snapshot


def test_reason_codes_distinct_nonempty_strings() -> None:
    """Коды причин — непустые строки, попарно различны (machine-readable)."""
    from disputatio.contracts.validation import (
        REASON_APPROVE_ON_FAILED_GATES,
        REASON_EMPTY_CHECKED,
        REASON_NO_SUBSTANTIVE_ISSUES,
    )

    codes = {
        REASON_NO_SUBSTANTIVE_ISSUES,
        REASON_APPROVE_ON_FAILED_GATES,
        REASON_EMPTY_CHECKED,
    }
    assert len(codes) == 3
    assert all(isinstance(code, str) and code for code in codes)
