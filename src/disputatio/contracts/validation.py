"""Кросс-артефактная валидация review.json ([DESIGN-008], [REQ-008…011]).

Чистые функции без I/O и мутаций — анти-галлюцинационное ядро §4.4
SPEC-001. Схемная валидация (pydantic, review.py) и протокольная
(этот модуль) не смешиваются: здесь Review уже схемно валиден.

Каждая `check_*` возвращает machine-readable код причины (константа
`REASON_*`) или `None`, если правило пройдено — оркестратор и тесты
сравнивают по константам, без строкового дублирования.
"""

from pydantic import Field

from disputatio.contracts.base import ArtifactChild
from disputatio.contracts.review import Issue, Review, Severity, Verdict
from disputatio.contracts.verification import OverallStatus, VerificationReport

REASON_NO_SUBSTANTIVE_ISSUES = "no_substantive_issues"
REASON_APPROVE_ON_FAILED_GATES = "approve_on_failed_gates"
REASON_EMPTY_CHECKED = "empty_checked"

_NEGATIVE_VERDICTS = (Verdict.REQUEST_CHANGES, Verdict.REJECT)
_SUBSTANTIVE_SEVERITIES = (Severity.BLOCKER, Severity.MAJOR)


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
    вместо отклонения ревью. Evidence из одних пробельных символов
    эквивалентен пустому — критерий `not evidence.strip()` (REQ-013).
    Возвращает новый объект (`model_copy`) и список id деградированных
    issues в исходном порядке; сам `review`, его вердикт и issues с
    содержательным `evidence` не затрагиваются.
    """
    degraded_ids: list[str] = []
    issues: list[Issue] = []
    for issue in review.issues:
        unevidenced = (
            issue.severity in (Severity.BLOCKER, Severity.MAJOR)
            and not issue.evidence.strip()
        )
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
    Элементы из одних пробельных символов не считаются: `checked` принят,
    только если хотя бы один элемент проходит `item.strip()` (REQ-013);
    `[]` и `["   ", "\\t"]` дают один и тот же код причины.
    """
    substantive = any(item.strip() for item in review.checked)
    return None if substantive else REASON_EMPTY_CHECKED
