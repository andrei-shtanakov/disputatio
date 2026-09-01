"""Роль findings-item и разрешённый чеклист: SPEC-002 v0.2 §5.2 V1/V8, §5.3.

Редакция v0.1 искала в правиле V8 литерал `S1`. Для контура `pair` такого id
нет вовсе (`PAIR_CHECKLIST` — это P1–P5), значит правило не срабатывало ни
разу, и нигде это не было заявлено. Здесь пустота роли проверяется как
ОБЪЯВЛЕННОЕ поведение, а не как совпадение, — в этом и разница между
записанным `None` и ненайденным именем.

Фикстуры моделей берутся из `test_doc_review_validation.py`: набор правил
один, и вторая копия конструкторов `Review`/`VerificationReport` разошлась
бы с первой ровно там, где расхождение труднее всего заметить.
"""

from contracts.test_doc_review_validation import (
    make_checklist_item,
    make_full_checklist,
    make_issue,
    make_review,
    make_verification,
)
from disputatio.contracts.checklists_catalog import (
    CHECKLIST_BY_CONTOUR,
    CHECKLIST_TEXT,
    FINDINGS_ITEM_BY_CONTOUR,
    ResolvedChecklist,
)
from disputatio.contracts.validation import (
    REASON_CHECKLIST_CONTRADICTS_ISSUES,
    REASON_CHECKLIST_ID_MISMATCH,
    validate_doc_review,
)


def _resolved(contour: str) -> ResolvedChecklist:
    """Разрешённый чеклист встроенного контура — вендоренный состав."""
    order = CHECKLIST_BY_CONTOUR[contour]
    return ResolvedChecklist(
        order=order,
        texts={item_id: CHECKLIST_TEXT[item_id] for item_id in order},
        findings_item=FINDINGS_ITEM_BY_CONTOUR[contour],
    )


#: Операторский чеклист контура `doc`: состав и роль объявил бы конфиг, а
#: вендоренного дефолта у него нет и быть не может (§5.3).
_DOC_CHECKLIST = ResolvedChecklist(
    order=("B1", "B3"),
    texts={
        "B1": "каждый BEH-NN несёт traces:",
        "B3": "нет blocker/major-находок",
    },
    findings_item="B3",
)


# --- роль контура (§5.3) ----------------------------------------------


def test_pair_findings_item_is_explicitly_absent() -> None:
    """Пустота роли у pair — записанное утверждение, а не ненайденный литерал."""
    assert "pair" in FINDINGS_ITEM_BY_CONTOUR
    assert FINDINGS_ITEM_BY_CONTOUR["pair"] is None


def test_spec_findings_item_is_s1() -> None:
    assert FINDINGS_ITEM_BY_CONTOUR["spec"] == "S1"


# --- V8 судит по роли, а не по имени (§5.2) ---------------------------


def test_v8_fires_on_role_item_not_on_literal_s1() -> None:
    """Правило проверяет назначенный пункт, а имени S1 не знает."""
    review = make_review(
        issues=[make_issue("R1-1", "blocker")],
        checklist=make_full_checklist(_DOC_CHECKLIST.order),
    )
    assert REASON_CHECKLIST_CONTRADICTS_ISSUES in validate_doc_review(
        review,
        contour="doc",
        checklist=_DOC_CHECKLIST,
        verification=make_verification(),
    )


def test_v8_silent_for_contour_without_role() -> None:
    """Пустая роль — законное бездействие, и оно проверено как объявленное."""
    checklist = _resolved("pair")
    review = make_review(
        issues=[make_issue("R1-1", "blocker", defect_class="execution")],
        checklist=make_full_checklist(checklist.order),
    )
    assert REASON_CHECKLIST_CONTRADICTS_ISSUES not in validate_doc_review(
        review,
        contour="pair",
        checklist=checklist,
        verification=make_verification(),
    )


def test_v8_still_fires_for_spec_exactly_as_in_v01() -> None:
    """Регрессия §10: для контура spec поведение прежнее, роль — `S1`."""
    checklist = _resolved("spec")
    review = make_review(
        issues=[make_issue("R1-1", "major")],
        checklist=make_full_checklist(checklist.order),
    )
    assert REASON_CHECKLIST_CONTRADICTS_ISSUES in validate_doc_review(
        review,
        contour="spec",
        checklist=checklist,
        verification=make_verification(),
    )


def test_v8_lets_the_role_item_fail_honestly() -> None:
    """`fail` у назначенного пункта противоречия не создаёт — он его признаёт."""
    review = make_review(
        issues=[make_issue("R1-1", "blocker")],
        checklist=make_full_checklist(
            _DOC_CHECKLIST.order,
            B3=make_checklist_item("B3", status="fail", issue_ids=["R1-1"]),
        ),
    )
    assert REASON_CHECKLIST_CONTRADICTS_ISSUES not in validate_doc_review(
        review,
        contour="doc",
        checklist=_DOC_CHECKLIST,
        verification=make_verification(),
    )


# --- V1 судит по разрешённому набору, а не по глобальному каталогу ----


def test_v1_uses_resolved_set_not_global_catalog() -> None:
    """Набор операторского контура глобальной константой не описан вовсе."""
    review = make_review(
        verdict="approve",
        checklist=make_full_checklist(_DOC_CHECKLIST.order),
    )
    assert (
        validate_doc_review(
            review,
            contour="doc",
            checklist=_DOC_CHECKLIST,
            verification=make_verification(),
        )
        == []
    )


def test_v1_rejects_id_outside_resolved_set() -> None:
    """Чужой id отвергается и у операторского контура — набор закрыт (V1)."""
    review = make_review(
        verdict="approve",
        checklist=make_full_checklist(("B1", "S1")),
    )
    assert REASON_CHECKLIST_ID_MISMATCH in validate_doc_review(
        review,
        contour="doc",
        checklist=_DOC_CHECKLIST,
        verification=make_verification(),
    )


def test_v5_stays_pair_only_for_operator_contour() -> None:
    """V5 неприменим к `doc`: маршрута возврата у него нет (§5.2, P10).

    Требовать `defect_class` там, где возвращаться некуда, значило бы просить
    ревьюера заполнить поле, которое никто не читает.
    """
    review = make_review(
        issues=[make_issue("R1-1", "blocker")],
        checklist=make_full_checklist(
            _DOC_CHECKLIST.order,
            B3=make_checklist_item("B3", status="fail", issue_ids=["R1-1"]),
        ),
    )
    assert (
        validate_doc_review(
            review,
            contour="doc",
            checklist=_DOC_CHECKLIST,
            verification=make_verification(),
        )
        == []
    )
