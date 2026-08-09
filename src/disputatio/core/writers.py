"""Активный писатель (инвариант I1) — единый источник истины (DESIGN-002).

`Role` из contracts не расширяется (в нём нет оркестратора) [NFR-004]: ядро
вводит собственный enum `Writer`. Писать может либо автор (`PROPOSING`), либо
оркестратор (все прочие активные состояния); в `IDLE`/`DONE`/`FAILED` писателя
нет. Ревьюер писателем не бывает никогда — он строго read-only (§7 SPEC-001).
"""

from collections.abc import Mapping
from enum import StrEnum

from disputatio.contracts import SessionPhase


class Writer(StrEnum):
    """Кто вправе писать в рабочую директорию в данном состоянии."""

    AUTHOR = "author"
    ORCHESTRATOR = "orchestrator"


ACTIVE_WRITER: Mapping[SessionPhase, Writer | None] = {
    SessionPhase.IDLE: None,
    SessionPhase.PROPOSING: Writer.AUTHOR,
    SessionPhase.VERIFYING: Writer.ORCHESTRATOR,
    SessionPhase.REVIEWING: Writer.ORCHESTRATOR,
    SessionPhase.DECIDING: Writer.ORCHESTRATOR,
    SessionPhase.CONVERGED: Writer.ORCHESTRATOR,
    SessionPhase.DEADLOCK: Writer.ORCHESTRATOR,
    SessionPhase.BUDGET_HIT: Writer.ORCHESTRATOR,
    SessionPhase.ESCALATED: Writer.ORCHESTRATOR,
    SessionPhase.EXPORTING: Writer.ORCHESTRATOR,
    SessionPhase.DONE: None,
    SessionPhase.FAILED: None,
}


def active_writer(phase: SessionPhase) -> Writer | None:
    """Возвращает единственного писателя фазы либо `None`, если писателя нет."""
    return ACTIVE_WRITER[phase]
