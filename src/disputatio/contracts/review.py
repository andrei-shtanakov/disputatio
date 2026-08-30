"""Модель `review.json` — Review, Issue, Verdict, Severity ([DESIGN-005], [REQ-005]).

Схема §4.4 SPEC-001 — только схемная валидация. Кросс-артефактные правила
(деградация blocker|major без evidence, запрет approve при
`verification.overall == fail`, «пустой checked ⇒ ревью не принято») живут
в validation.py: схемная валидация (ретрай агента с текстом ошибки
pydantic) и протокольная валидация (ретрай ревью) не смешиваются.

`checklist` (Review) и `defect_class` (Issue) — расширения `disputatio/v2`
(SPEC-002 §5.1, §5.2, doc-сессии). Они объявлены optional'ными аддитивно
ко всем Review, но допустимы только под тегом v2: `_forbid_v2_fields_in_v1`
ниже отвергает их под v1, иначе optional-поля тихо расширили бы принимаемый
v1-payload мимо `extra="forbid"` (он ловит только неизвестные ключи, а эти
стали бы известными).
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from disputatio.contracts.base import SCHEMA_V1, ArtifactBase, ArtifactChild, Role
from disputatio.contracts.checklist import ChecklistItem


class Verdict(StrEnum):
    """Вердикт ревьюера (§4.4)."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class Severity(StrEnum):
    """Серьёзность issue (§4.4)."""

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


class Issue(ArtifactChild):
    """Одно замечание ревьюера (§4.4 элемент `issues`).

    Пустая строка `evidence` (дефолт) означает «нет evidence» — сигнал
    деградации REQ-009 в validation.py, не ошибка схемы.
    """

    id: str
    severity: Severity
    file: str
    line_hint: int | None = None
    claim: str
    evidence: str = ""
    suggestion: str | None = None
    defect_class: Literal["architectural", "execution"] | None = None


class Review(ArtifactBase):
    """Корневой артефакт `review.json` (§4.4 SPEC-001).

    `role` — Literal: review пишет только ревьюер, иная роль отклоняется
    схемой. `checked` обязателен, но пустой список схемно валиден —
    правило «пустой checked ⇒ не принято» живёт в validation.py (REQ-011).
    """

    round: int = Field(ge=1)
    role: Literal[Role.REVIEWER]
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    issues: list[Issue] = Field(default_factory=list)
    checked: list[str]
    summary: str
    checklist: list[ChecklistItem] | None = None

    @model_validator(mode="after")
    def _forbid_v2_fields_in_v1(self) -> "Review":
        if self.schema_ != SCHEMA_V1:
            return self
        if self.checklist is not None:
            raise ValueError(
                "checklist недопустим в disputatio/v1: поле относится "
                "к disputatio/v2 (SPEC-002 §5.1)"
            )
        if any(issue.defect_class is not None for issue in self.issues):
            raise ValueError(
                "issues[].defect_class недопустим в disputatio/v1: поле "
                "относится к disputatio/v2 (SPEC-002 §5.1)"
            )
        return self
