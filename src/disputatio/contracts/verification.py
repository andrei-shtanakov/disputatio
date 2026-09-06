"""Модель `verification.json` — VerificationReport ([DESIGN-004], [REQ-004]).

Схема §4.3 SPEC-001. `overall` — отдельный enum `OverallStatus`: `"skip"`
на верхнем уровне отклоняется типом, без кастомного валидатора. Правило
«`overall: fail` не блокирует REVIEWING, но блокирует CONVERGED» — забота
оркестратора (§5.1), не модели.
"""

from enum import StrEnum

from pydantic import Field

from disputatio.contracts.base import ArtifactBase, ArtifactChild


class GateStatus(StrEnum):
    """Статус одного детерминированного гейта."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class OverallStatus(StrEnum):
    """Итог verification (§4.3): `skip` недопустим типом.

    `INDETERMINATE` — не провал и не успех, а отсутствие свидетельства:
    ни один гейт не выполнен (набор пуст или состоит из одних `skip`).
    Наружу ведёт себя как `fail` — сходимость блокирует, переход в
    `REVIEWING` нет, — но не выдаёт «проверок не было» за «проверка
    провалилась». Значение добавлено в `disputatio/v1` расширением:
    артефакты прежних сессий читаются без изменений (§4.3).
    """

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class GateResult(ArtifactChild):
    """Результат одного гейта (§4.3 элемент `gates`)."""

    name: str
    cmd: str
    status: GateStatus
    exit_code: int | None = None
    duration_s: float | None = None
    tail: str = ""
    reason: str | None = None


class DiffStats(ArtifactChild):
    """Статистика `git diff` раунда (§4.3 `diff_stats`)."""

    files: int
    insertions: int
    deletions: int


class VerificationReport(ArtifactBase):
    """Корневой артефакт `verification.json` (§4.3 SPEC-001)."""

    round: int = Field(ge=1)
    gates: list[GateResult] = Field(default_factory=list)
    overall: OverallStatus
    diff_stats: DiffStats
