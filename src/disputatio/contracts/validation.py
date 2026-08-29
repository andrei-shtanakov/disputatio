"""Кросс-артефактная валидация review.json ([DESIGN-008], [REQ-008…011]).

Чистые функции без I/O и мутаций — анти-галлюцинационное ядро §4.4
SPEC-001. Схемная валидация (pydantic, review.py) и протокольная
(этот модуль) не смешиваются: здесь Review уже схемно валиден.

Каждая `check_*` возвращает machine-readable код причины (константа
`REASON_*`) или `None`, если правило пройдено — оркестратор и тесты
сравнивают по константам, без строкового дублирования.
"""

import unicodedata
from typing import Literal

from pydantic import Field

from disputatio.contracts.base import ArtifactChild
from disputatio.contracts.checklists_catalog import CHECKLIST_BY_CONTOUR
from disputatio.contracts.review import Issue, Review, Severity, Verdict
from disputatio.contracts.verification import OverallStatus, VerificationReport

REASON_NO_SUBSTANTIVE_ISSUES = "no_substantive_issues"
REASON_APPROVE_ON_FAILED_GATES = "approve_on_failed_gates"
REASON_EMPTY_CHECKED = "empty_checked"

# SPEC-002 §5.2, doc-ревью (Mode.DOCUMENT, validate_doc_review ниже).
REASON_CHECKLIST_ID_MISMATCH = "checklist_id_mismatch"
REASON_APPROVE_WITH_CHECKLIST_FAIL = "approve_with_checklist_fail"
REASON_CHECKLIST_FAIL_WITHOUT_ISSUE_IDS = "checklist_fail_without_issue_ids"
REASON_CHECKLIST_FAIL_UNKNOWN_ISSUE_ID = "checklist_fail_unknown_issue_id"
REASON_CHECKLIST_FAIL_ISSUE_SEVERITY_TOO_LOW = "checklist_fail_issue_severity_too_low"
REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS = "pair_issue_missing_defect_class"
REASON_APPROVE_WITH_SUBSTANTIVE_ISSUE = "approve_with_substantive_issue"
REASON_CHECKLIST_PASS_CONTRADICTS_S1 = "checklist_pass_contradicts_s1"

_NEGATIVE_VERDICTS = (Verdict.REQUEST_CHANGES, Verdict.REJECT)
_SUBSTANTIVE_SEVERITIES = (Severity.BLOCKER, Severity.MAJOR)


def _semantic_text(text: str) -> str:
    """Семантическое содержимое строки: без Cf-символов и краевых пробелов.

    Удаляет все символы Unicode-категории Cf (U+200B, U+FEFF и др.)
    ДО strip: строка из одних невидимых и/или пробельных символов
    нормализуется в "".
    """
    visible = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return visible.strip()


class ReviewAcceptance(ArtifactChild):
    """Результат конвейера валидации ревью (§4.4, [DESIGN-008]).

    Данные, не действия: оркестратор по `accepted` решает, принимать ли
    ревью или ретраить с `rejection_reasons` (machine-readable коды).
    """

    accepted: bool
    review: Review
    degraded_issue_ids: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)


def validate_review(
    review: Review, verification: VerificationReport
) -> ReviewAcceptance:
    """Конвейер §4.4 (ADR-003): деградация REQ-009, затем все проверки.

    Порядок фиксирован — `degrade_unevidenced_issues` выполняется ДО
    `check_substantive_issues`: если деградация сняла последний
    blocker|major, негативный вердикт отклоняется уже здесь. Все check_*
    работают на деградированной модели, коды причин накапливаются ВСЕ,
    не только первый — ревьюеру при ретрае отдаётся полный список.
    Входные модели не мутируются; `ReviewAcceptance.review` — возможно
    деградированная копия.

    Вход ревалидируется (REQ-015): модели, собранные
    `model_copy(update=...)` со строками вместо enum, нормализуются к
    членам enum. Схемно-невалидный вход поднимает `ValidationError` —
    контракт конвейера; обрабатывается слоем schema-retry оркестратора.
    """
    review = Review.model_validate(review.model_dump(by_alias=True))
    verification = VerificationReport.model_validate(
        verification.model_dump(by_alias=True)
    )
    degraded, degraded_ids = degrade_unevidenced_issues(review)
    checks = (
        check_substantive_issues(degraded),
        check_verdict_vs_verification(degraded, verification),
        check_checked_nonempty(degraded),
    )
    reasons = [reason for reason in checks if reason is not None]
    return ReviewAcceptance(
        accepted=not reasons,
        review=degraded,
        degraded_issue_ids=degraded_ids,
        rejection_reasons=reasons,
    )


def degrade_unevidenced_issues(review: Review) -> tuple[Review, list[str]]:
    """REQ-009: blocker|major с пустым evidence → НОВЫЙ Review с minor.

    Голословный блокер не крутит цикл: issue деградируется до `minor`
    вместо отклонения ревью. Evidence из одних пробельных и/или
    невидимых Cf-символов (U+200B, U+FEFF) эквивалентен пустому —
    критерий `not _semantic_text(evidence)` (REQ-013 + Cf-hardening).
    Возвращает новый объект (`model_copy`) и список id деградированных
    issues в исходном порядке; сам `review`, его вердикт и issues с
    содержательным `evidence` не затрагиваются — нормализация только
    для критерия, текст evidence в копии хранится как пришёл.
    """
    degraded_ids: list[str] = []
    issues: list[Issue] = []
    for issue in review.issues:
        unevidenced = issue.severity in (
            Severity.BLOCKER,
            Severity.MAJOR,
        ) and not _semantic_text(issue.evidence)
        if unevidenced:
            issues.append(issue.model_copy(update={"severity": Severity.MINOR}))
            degraded_ids.append(issue.id)
        else:
            issues.append(issue)
    return review.model_copy(update={"issues": issues}), degraded_ids


def check_substantive_issues(review: Review) -> str | None:
    """REQ-008: request_changes|reject без ≥1 blocker|major → код причины.

    Негативный вердикт обязан быть обоснован существенным issue; approve
    это правило не трогает. Вызывается ПОСЛЕ деградации REQ-009 — если
    деградация сняла последний blocker|major, ревью отклоняется здесь.
    """
    if review.verdict not in _NEGATIVE_VERDICTS:
        return None
    substantive = any(
        issue.severity in _SUBSTANTIVE_SEVERITIES for issue in review.issues
    )
    return None if substantive else REASON_NO_SUBSTANTIVE_ISSUES


def check_verdict_vs_verification(
    review: Review, verification: VerificationReport
) -> str | None:
    """REQ-010: approve при `verification.overall == fail` → код причины.

    Исключает противоречие «одобряю, но gates красные»; остальные
    вердикты при fail пропускаются — ревьюер взвешивает провал сам (§5.1).
    Сравнение через `==`, не `is` (REQ-015): StrEnum равен и члену, и
    строке — standalone-вызов со строковыми значениями работает идентично.
    """
    approve_on_fail = (
        review.verdict == Verdict.APPROVE and verification.overall == OverallStatus.FAIL
    )
    return REASON_APPROVE_ON_FAILED_GATES if approve_on_fail else None


def check_checked_nonempty(review: Review) -> str | None:
    """REQ-011: `checked` без содержательных элементов → код причины.

    Пустой список схемно валиден (review.py), но ревью без единого
    осмотренного объекта не принимается — дешёвый прокси верифицируемости.
    Элементы из одних пробельных и/или невидимых Cf-символов не
    считаются: `checked` принят, только если хотя бы один элемент
    проходит `_semantic_text(item)` (REQ-013 + Cf-hardening); `[]`,
    `["   ", "\\t"]` и `["\\u200b"]` дают один и тот же код причины.
    """
    substantive = any(_semantic_text(item) for item in review.checked)
    return None if substantive else REASON_EMPTY_CHECKED


def validate_doc_review(
    review: Review,
    *,
    contour: Literal["spec", "pair"],
    verification: VerificationReport,
) -> list[str]:
    """SPEC-002 §5.2, правила V1–V4, V5, V7, V8 doc-ревью (Mode.DOCUMENT).

    Анти-галлюцинационное ядро §4.4 SPEC-001, специализация для doc-сессий:
    `checklist` обязан покрыть ровно контурный набор id (V1), быть
    непротиворечив с вердиктом (V3, V7) и с issues этого же ревью (V4, V8);
    pair-контур дополнительно требует `defect_class` на каждой существенной
    находке (V5). V2 (evidence непуст) закрыта типом `ChecklistItem` (задача
    1) и здесь не дублируется; V6 — следствие V1–V4, V7, не отдельная
    проверка.

    **Порядок вызова в конвейере фиксирован (§5.2 SPEC-002): эта функция
    обязана получать `review` ДО `degrade_unevidenced_issues`.** Правила
    V5/V7/V8 сравнивают severity issues напрямую — деградация REQ-009
    понижает безевиденсный blocker/major до `minor` и стирает именно тот
    сигнал, который эти правила обязаны увидеть. Функция сама деградацию не
    вызывает и не мутирует вход — она читает `review` таким, каким его
    получила; ответственность за порядок — на вызывающем коде.

    `verification` — для симметрии с конвейером §4.4 (`validate_review`) и
    будущих doc-гейтов, привязанных к результатам verification; V1–V8 её не
    используют.

    Возвращает список machine-readable кодов причин (`REASON_*`); пустой
    список означает, что doc-специфичные правила пройдены.
    """
    del verification  # не используется V1-V8 (см. докстринг)
    errors: list[str] = []
    checklist = list(review.checklist or [])
    expected_ids = set(CHECKLIST_BY_CONTOUR[contour])
    actual_ids = [item.id for item in checklist]

    if set(actual_ids) != expected_ids or len(actual_ids) != len(expected_ids):
        errors.append(REASON_CHECKLIST_ID_MISMATCH)

    issues_by_id = {issue.id: issue for issue in review.issues}
    substantive_issues = [
        issue for issue in review.issues if issue.severity in _SUBSTANTIVE_SEVERITIES
    ]

    if review.verdict == Verdict.APPROVE and any(
        item.status == "fail" for item in checklist
    ):
        errors.append(REASON_APPROVE_WITH_CHECKLIST_FAIL)

    for item in checklist:
        if item.status != "fail":
            continue
        if not item.issue_ids:
            errors.append(REASON_CHECKLIST_FAIL_WITHOUT_ISSUE_IDS)
            continue
        for issue_id in item.issue_ids:
            linked = issues_by_id.get(issue_id)
            if linked is None:
                errors.append(REASON_CHECKLIST_FAIL_UNKNOWN_ISSUE_ID)
            elif linked.severity not in _SUBSTANTIVE_SEVERITIES:
                errors.append(REASON_CHECKLIST_FAIL_ISSUE_SEVERITY_TOO_LOW)

    if contour == "pair":
        errors.extend(
            REASON_PAIR_ISSUE_MISSING_DEFECT_CLASS
            for issue in substantive_issues
            if issue.defect_class is None
        )

    if review.verdict == Verdict.APPROVE and substantive_issues:
        errors.append(REASON_APPROVE_WITH_SUBSTANTIVE_ISSUE)

    s1 = next((item for item in checklist if item.id == "S1"), None)
    if s1 is not None and s1.status == "pass" and substantive_issues:
        errors.append(REASON_CHECKLIST_PASS_CONTRADICTS_S1)

    return errors
