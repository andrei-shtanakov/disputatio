"""Тесты validate_doc_review: SPEC-002 §5.2 (правила V1–V8), §5.3.

Импорт `validate_doc_review` внутри тестов: на момент red-чекпоинта функции
ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_v1_checklist_id_mismatch_rejected`) превращает ImportError в
AssertionError. Все тесты — на голых моделях, без диска и моков
([DESIGN-008]).

Критично: `validate_doc_review` обязана видеть severity issues ДО
деградации REQ-009 (`degrade_unevidenced_issues`, validation.py) — все
фикстуры ниже строят `review` напрямую, без прогона через деградацию,
кроме `test_doc_rules_see_severity_before_degrade`, который это
проверяет явно (см. §5.2 SPEC-002).
"""

import itertools
from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.checklist import ChecklistItem
from disputatio.contracts.review import Review
from disputatio.contracts.verification import VerificationReport

SPEC_IDS = ("S1", "S2", "S3", "S4", "S5")
PAIR_IDS = ("P1", "P2", "P3", "P4", "P5")


def make_issue(
    issue_id: str,
    severity: str,
    *,
    evidence: str = "подтверждено: цитата diff",
    defect_class: str | None = None,
) -> dict[str, Any]:
    """Payload одного issue; `evidence` непустой по умолчанию."""
    return {
        "id": issue_id,
        "severity": severity,
        "file": "src/x.py",
        "claim": "что не так, проверяемая формулировка",
        "evidence": evidence,
        "defect_class": defect_class,
    }


def make_checklist_item(
    item_id: str,
    *,
    status: str = "pass",
    issue_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Payload одного ChecklistItem с валидной gate-evidence по умолчанию."""
    return {
        "id": item_id,
        "status": status,
        "evidence": [{"kind": "gate", "ref": "doc-links"}],
        "issue_ids": [] if issue_ids is None else issue_ids,
    }


def make_full_checklist(
    ids: tuple[str, ...], **overrides: dict[str, Any]
) -> list[dict[str, Any]]:
    """Полный чеклист контура: все пункты `pass`, кроме перечисленных в overrides."""
    return [overrides.get(item_id, make_checklist_item(item_id)) for item_id in ids]


def make_review(
    *,
    issues: list[dict[str, Any]] | None = None,
    checklist: list[dict[str, Any]] | None = None,
    verdict: str = "request_changes",
    checked: list[str] | None = None,
) -> Review:
    """Review со схемой `disputatio/v2` — вход validate_doc_review."""
    return Review.model_validate(
        {
            "schema": "disputatio/v2",
            "round": 3,
            "role": "reviewer",
            "verdict": verdict,
            "confidence": 0.8,
            "issues": [] if issues is None else issues,
            "checked": ["прочитал diff"] if checked is None else checked,
            "summary": "1-3 предложения",
            "checklist": checklist,
        }
    )


def make_verification(overall: str = "pass") -> VerificationReport:
    """VerificationReport с заданным overall — параметр сигнатуры, не используется V1-V8."""
    return VerificationReport.model_validate(
        {
            "schema": "disputatio/v1",
            "round": 3,
            "gates": [],
            "overall": overall,
            "diff_stats": {"files": 1, "insertions": 2, "deletions": 0},
        }
    )


def test_v1_checklist_id_mismatch_rejected() -> None:
    """V1: пропуск id и чужой id — обе формы дают ошибку."""
    try:
        from disputatio.contracts.validation import (
            REASON_CHECKLIST_ID_MISMATCH,
            validate_doc_review,
        )
    except ImportError as exc:  # red-фаза: validate_doc_review ещё не создан
        raise AssertionError("validate_doc_review ещё не реализована") from exc

    missing_one = make_review(
        verdict="approve",
        checklist=make_full_checklist(SPEC_IDS[:-1]),  # без S5
    )
    wrong_id = make_review(
        verdict="approve",
        checklist=[
            *make_full_checklist(SPEC_IDS[:-1]),
            make_checklist_item("P1"),  # чужой id вместо S5
        ],
    )
    for review in (missing_one, wrong_id):
        errors = validate_doc_review(
            review, contour="spec", verification=make_verification()
        )
        assert REASON_CHECKLIST_ID_MISMATCH in errors


def test_v2_checklist_evidence_required_by_schema() -> None:
    """V2: пустой evidence у пункта отклоняется схемой раньше validate_doc_review.

    Регрессия на инвариант задачи 1 (`ChecklistItem.evidence` — min_length=1):
    голословный pass/not_applicable не может даже попасть в Review.
    """
    with pytest.raises(ValidationError):
        ChecklistItem.model_validate(
            {"id": "S1", "status": "pass", "evidence": [], "issue_ids": []}
        )


def test_v3_approve_with_checklist_fail_rejected() -> None:
    """V3: approve несовместим с любым пунктом чеклиста в статусе fail."""
    from disputatio.contracts.validation import (
        REASON_APPROVE_WITH_CHECKLIST_FAIL,
        validate_doc_review,
    )

    review = make_review(
        verdict="approve",
        issues=[make_issue("R3-1", "major")],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_APPROVE_WITH_CHECKLIST_FAIL in errors


def test_v4_fail_without_issue_ids_rejected() -> None:
    """V4: fail с пустым issue_ids — ошибка (нечем двигать цикл)."""
    from disputatio.contracts.validation import (
        REASON_CHECKLIST_FAIL_WITHOUT_ISSUE_IDS,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="fail", issue_ids=[]),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_CHECKLIST_FAIL_WITHOUT_ISSUE_IDS in errors


def test_v4_fail_referencing_unknown_issue_rejected() -> None:
    """V4: fail ссылается на issue_id, которого нет в этом ревью — ошибка."""
    from disputatio.contracts.validation import (
        REASON_CHECKLIST_FAIL_UNKNOWN_ISSUE_ID,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="fail", issue_ids=["NOPE"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_CHECKLIST_FAIL_UNKNOWN_ISSUE_ID in errors


def test_v4_fail_referencing_low_severity_issue_rejected() -> None:
    """V4: fail ссылается на существующий issue, но его severity < major — ошибка."""
    from disputatio.contracts.validation import (
        REASON_CHECKLIST_FAIL_ISSUE_SEVERITY_TOO_LOW,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "minor")],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_CHECKLIST_FAIL_ISSUE_SEVERITY_TOO_LOW in errors


def test_v4_valid_fail_link_passes() -> None:
    """V4: fail с непустым issue_ids, существующим и severity >= major — ок."""
    from disputatio.contracts.validation import (
        REASON_CHECKLIST_FAIL_ISSUE_SEVERITY_TOO_LOW,
        REASON_CHECKLIST_FAIL_UNKNOWN_ISSUE_ID,
        REASON_CHECKLIST_FAIL_WITHOUT_ISSUE_IDS,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "blocker")],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_CHECKLIST_FAIL_WITHOUT_ISSUE_IDS not in errors
    assert REASON_CHECKLIST_FAIL_UNKNOWN_ISSUE_ID not in errors
    assert REASON_CHECKLIST_FAIL_ISSUE_SEVERITY_TOO_LOW not in errors


def test_v5_pair_contour_requires_defect_class_on_substantive_issue() -> None:
    """V5: pair-контур — каждый blocker/major без defect_class → ошибка."""
    from disputatio.contracts.validation import (
        REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "blocker", defect_class=None)],
        checklist=make_full_checklist(
            PAIR_IDS,
            P1=make_checklist_item("P1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="pair", verification=make_verification()
    )
    assert REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS in errors


def test_v5_pair_contour_passes_with_defect_class_set() -> None:
    """V5: blocker/major c defect_class проставленным — правило не срабатывает."""
    from disputatio.contracts.validation import (
        REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "blocker", defect_class="execution")],
        checklist=make_full_checklist(
            PAIR_IDS,
            P1=make_checklist_item("P1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="pair", verification=make_verification()
    )
    assert REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS not in errors


def test_v5_spec_contour_not_checked() -> None:
    """V5 — правило только pair-контура; spec с тем же дефектом его не видит."""
    from disputatio.contracts.validation import (
        REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "blocker", defect_class=None)],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS not in errors


def test_v7_approve_with_substantive_issue_rejected() -> None:
    """V7: Mode.DOCUMENT — approve несовместим с наличием blocker/major issue."""
    from disputatio.contracts.validation import (
        REASON_APPROVE_WITH_SUBSTANTIVE_ISSUE,
        validate_doc_review,
    )

    review = make_review(
        verdict="approve",
        issues=[make_issue("R3-1", "major")],
        checklist=make_full_checklist(SPEC_IDS),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_APPROVE_WITH_SUBSTANTIVE_ISSUE in errors


def test_v8_s1_pass_contradicts_substantive_issue() -> None:
    """V8: `S1: pass` несовместим с blocker/major issues этого ревью."""
    from disputatio.contracts.validation import (
        REASON_CHECKLIST_PASS_CONTRADICTS_S1,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "blocker")],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="pass"),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_CHECKLIST_PASS_CONTRADICTS_S1 in errors


def test_v8_s1_fail_with_substantive_issue_passes() -> None:
    """V8: `S1: fail`, отражающий тот же blocker, — не противоречие."""
    from disputatio.contracts.validation import (
        REASON_CHECKLIST_PASS_CONTRADICTS_S1,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "blocker")],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_CHECKLIST_PASS_CONTRADICTS_S1 not in errors


@pytest.mark.parametrize(
    "statuses",
    [
        combo
        for combo in itertools.product(("pass", "fail", "not_applicable"), repeat=5)
        if "fail" in combo
    ],
)
def test_v3_property_approve_never_passes_with_any_fail_item(
    statuses: tuple[str, ...],
) -> None:
    """Property: любая комбинация с ≥1 fail-пунктом никогда не проходит с approve."""
    from disputatio.contracts.validation import (
        REASON_APPROVE_WITH_CHECKLIST_FAIL,
        validate_doc_review,
    )

    checklist = [
        make_checklist_item(item_id, status=status)
        for item_id, status in zip(SPEC_IDS, statuses, strict=True)
    ]
    review = make_review(verdict="approve", issues=[], checklist=checklist)
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert REASON_APPROVE_WITH_CHECKLIST_FAIL in errors


def test_doc_rules_see_severity_before_degrade() -> None:
    """Критичный тест порядка: V5/V7/V8 видят исходную severity, до деградации.

    `approve` + blocker без evidence + `S1: pass` + отсутствующий
    `defect_class` — деградация REQ-009 понизила бы blocker до minor и
    сняла бы сигнал для V5/V7/V8. `validate_doc_review` обязана
    отклонить это ревью, работая на исходном (недеградированном) review.
    """
    from disputatio.contracts.validation import (
        REASON_APPROVE_WITH_SUBSTANTIVE_ISSUE,
        REASON_CHECKLIST_PASS_CONTRADICTS_S1,
        REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS,
        degrade_unevidenced_issues,
        validate_doc_review,
    )

    # V8 машинно завязан на id "S1" независимо от контура (см.
    # test_v8_s1_check_is_spec_only_id для обратного случая): здесь он
    # добавлен поверх полного pair-чеклиста ровно для того, чтобы
    # воспроизвести сценарий брифа буквально — V5, V7 и V8 срабатывают
    # на одном и том же ревью одновременно.
    review = make_review(
        verdict="approve",
        issues=[make_issue("R3-1", "blocker", evidence="", defect_class=None)],
        checklist=[
            *make_full_checklist(PAIR_IDS, P1=make_checklist_item("P1", status="pass")),
            make_checklist_item("S1", status="pass"),
        ],
    )
    # Санити: деградация действительно понизила бы этот blocker до minor.
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    assert degraded_ids == ["R3-1"]
    assert degraded.issues[0].severity == "minor"

    # validate_doc_review получает ИСХОДНЫЙ review (до деградации) — правила
    # обязаны сработать, несмотря на то что деградация сняла бы сигнал.
    errors = validate_doc_review(
        review, contour="pair", verification=make_verification()
    )
    assert REASON_APPROVE_WITH_SUBSTANTIVE_ISSUE in errors  # V7
    assert REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS in errors  # V5
    assert REASON_CHECKLIST_PASS_CONTRADICTS_S1 in errors  # V8

    # А вот прогон на уже деградированной копии эти правила бы не поймал —
    # именно поэтому порядок в конвейере зафиксирован §5.2 SPEC-002.
    errors_on_degraded = validate_doc_review(
        degraded, contour="pair", verification=make_verification()
    )
    assert REASON_APPROVE_WITH_SUBSTANTIVE_ISSUE not in errors_on_degraded
    assert REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS not in errors_on_degraded
    assert REASON_CHECKLIST_PASS_CONTRADICTS_S1 not in errors_on_degraded


def test_v8_s1_check_is_spec_only_id() -> None:
    """V8 машинно завязан на id `S1`; в pair-контуре такого id нет."""
    from disputatio.contracts.validation import (
        REASON_CHECKLIST_PASS_CONTRADICTS_S1,
        validate_doc_review,
    )

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "blocker", defect_class="execution")],
        checklist=make_full_checklist(
            PAIR_IDS,
            P1=make_checklist_item("P1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="pair", verification=make_verification()
    )
    assert REASON_CHECKLIST_PASS_CONTRADICTS_S1 not in errors


def test_valid_spec_review_passes_doc_validation() -> None:
    """Happy path: валидное spec-ревью с непустым checklist — errors пуст.

    Фикстура для дальнейших happy-path тестов (находка ревью задачи 1):
    полноценное `disputatio/v2` Review с заполненным чеклистом успешно
    проходит `validate_doc_review` и не теряет данные.
    """
    from disputatio.contracts.validation import validate_doc_review

    review = make_review(
        verdict="request_changes",
        issues=[make_issue("R3-1", "major")],
        checklist=make_full_checklist(
            SPEC_IDS,
            S1=make_checklist_item("S1", status="fail", issue_ids=["R3-1"]),
        ),
    )
    errors = validate_doc_review(
        review, contour="spec", verification=make_verification()
    )
    assert errors == []
    assert review.checklist is not None
    assert [item.id for item in review.checklist] == list(SPEC_IDS)


def test_valid_pair_review_passes_doc_validation() -> None:
    """Happy path: валидное pair-ревью (approve, чеклист весь pass) без ошибок."""
    from disputatio.contracts.validation import validate_doc_review

    review = make_review(
        verdict="approve",
        issues=[],
        checklist=make_full_checklist(PAIR_IDS),
    )
    errors = validate_doc_review(
        review, contour="pair", verification=make_verification()
    )
    assert errors == []
