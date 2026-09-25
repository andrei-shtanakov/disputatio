"""Модель `decision.json` — Decision, Outcome ([DESIGN-006], [REQ-006]).

Схема §4.5 SPEC-001 — только схемная валидация. Кросс-артефактные правила
(порядок проверки стоп-условий в `DECIDING`, «continue обязан нести
директиву», если такое правило появится) — слой оркестратора, не схема:
схемная валидация (ретрай агента с текстом ошибки pydantic) и протокольная
логика не смешиваются.
"""

from enum import StrEnum

from pydantic import Field

from disputatio.contracts.base import ArtifactBase, ArtifactChild


class Outcome(StrEnum):
    """Исход раунда, вынесенный оркестратором в `DECIDING` (§4.5, §5)."""

    CONVERGED = "converged"
    CONTINUE = "continue"
    DEADLOCK = "deadlock"
    BUDGET_HIT = "budget_hit"
    FAILED = "failed"


class BudgetSnapshot(ArtifactChild):
    """Накопленный `budget_used` на входе в `DECIDING` раунда (§4.5).

    Накопленный итог, а не стоимость раунда: стоимость выводится разностью
    соседних снимков (§5.2). `cost_usd_est` в снимок не входит — прогноз
    его не читает.
    """

    tokens: int = Field(ge=0)
    wall_seconds: float
    unreported_turns: int = Field(ge=0)


class Decision(ArtifactBase):
    """Корневой артефакт `decision.json` (§4.5 SPEC-001); пишет оркестратор.

    `reason` — machine-readable код (`approve_with_gates_pass`,
    `max_rounds`, `oscillation`, …); свободная строка, реестр кодов —
    не схема. `next_round_directive` обязателен как ключ, но nullable:
    `None` при terminal-исходе; схемно `None` допустим и при `continue`
    (кросс-артефактный слой решает, требовать ли директиву).

    `budget_snapshot` — расширение v1 (§4.5): новая версия пишет его в
    каждое решение, решение прежней версии читается с `None` и наблюдением
    стоимости раунда не служит. `None` не сериализуется: у решения без
    снимка ключа нет, как у артефакта прежней версии, а не `null`.
    """

    round: int = Field(ge=1)
    outcome: Outcome
    reason: str
    open_issues_carried: list[str] = Field(default_factory=list)
    next_round_directive: str | None
    budget_snapshot: BudgetSnapshot | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
