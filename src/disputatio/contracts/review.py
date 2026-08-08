"""Модель `review.json` — Review, Issue, Verdict, Severity ([DESIGN-005], [REQ-005]).

Схема §4.4 SPEC-001 — только схемная валидация. Кросс-артефактные правила
(деградация blocker|major без evidence, запрет approve при
`verification.overall == fail`, «пустой checked ⇒ ревью не принято») живут
в validation.py: схемная валидация (ретрай агента с текстом ошибки
pydantic) и протокольная валидация (ретрай ревью) не смешиваются.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from disputatio.contracts.base import ArtifactBase, ArtifactChild, Role


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
