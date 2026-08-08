"""Кросс-артефактная валидация review.json ([DESIGN-008], [REQ-009]).

Чистые функции без I/O и мутаций — анти-галлюцинационное ядро §4.4
SPEC-001. Схемная валидация (pydantic, review.py) и протокольная
(этот модуль) не смешиваются: здесь Review уже схемно валиден.
"""

from pydantic import Field

from disputatio.contracts.base import ArtifactChild
from disputatio.contracts.review import Issue, Review, Severity


class ReviewAcceptance(ArtifactChild):
    """Результат конвейера валидации ревью (§4.4, [DESIGN-008]).

    Данные, не действия: оркестратор по `accepted` решает, принимать ли
    ревью или ретраить с `rejection_reasons` (machine-readable коды).
    """

    accepted: bool
    review: Review
    degraded_issue_ids: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)


def degrade_unevidenced_issues(review: Review) -> tuple[Review, list[str]]:
    """REQ-009: blocker|major с пустым evidence → НОВЫЙ Review с minor.

    Голословный блокер не крутит цикл: issue деградируется до `minor`
    вместо отклонения ревью. Возвращает новый объект (`model_copy`) и
    список id деградированных issues в исходном порядке; сам `review`,
    его вердикт и issues с непустым `evidence` не затрагиваются.
    """
    degraded_ids: list[str] = []
    issues: list[Issue] = []
    for issue in review.issues:
        unevidenced = (
            issue.severity in (Severity.BLOCKER, Severity.MAJOR) and not issue.evidence
        )
        if unevidenced:
            issues.append(issue.model_copy(update={"severity": Severity.MINOR}))
            degraded_ids.append(issue.id)
        else:
            issues.append(issue)
    return review.model_copy(update={"issues": issues}), degraded_ids
