"""Граф переходов §2 SPEC-001 — единственный источник истины (DESIGN-001).

Рёбра — строго по таблице DESIGN-001, включая ADR-W2-01: `FAILED` достижим
из каждого активного состояния (`PROPOSING`, `VERIFYING`, `REVIEWING`,
`DECIDING`, `EXPORTING`). Возвратов внутри раунда (`REVIEWING → VERIFYING`
и т.п.) нет — статическая часть инварианта I3 [REQ-015]. Ребро
`VERIFYING → REVIEWING` безусловно: `verification.overall == fail` не
блокирует ревью [REQ-009].
"""

from collections.abc import Mapping

from disputatio.contracts import SessionPhase


class InvalidTransition(Exception):
    """Недопустимый переход; несёт оба состояния: `current` и `requested`."""

    def __init__(self, current: SessionPhase, requested: SessionPhase) -> None:
        super().__init__(f"недопустимый переход {current} → {requested}")
        self.current = current
        self.requested = requested


TRANSITIONS: Mapping[SessionPhase, frozenset[SessionPhase]] = {
    SessionPhase.IDLE: frozenset({SessionPhase.PROPOSING}),
    SessionPhase.PROPOSING: frozenset({SessionPhase.VERIFYING, SessionPhase.FAILED}),
    SessionPhase.VERIFYING: frozenset({SessionPhase.REVIEWING, SessionPhase.FAILED}),
    SessionPhase.REVIEWING: frozenset({SessionPhase.DECIDING, SessionPhase.FAILED}),
    SessionPhase.DECIDING: frozenset(
        {
            SessionPhase.PROPOSING,  # revise-петля
            SessionPhase.CONVERGED,
            SessionPhase.DEADLOCK,
            SessionPhase.BUDGET_HIT,
            SessionPhase.FAILED,
        }
    ),
    SessionPhase.CONVERGED: frozenset({SessionPhase.EXPORTING}),
    SessionPhase.DEADLOCK: frozenset({SessionPhase.ESCALATED}),
    SessionPhase.BUDGET_HIT: frozenset({SessionPhase.ESCALATED}),
    SessionPhase.ESCALATED: frozenset({SessionPhase.EXPORTING}),
    SessionPhase.EXPORTING: frozenset({SessionPhase.DONE, SessionPhase.FAILED}),
    SessionPhase.DONE: frozenset(),
    SessionPhase.FAILED: frozenset(),
}

TERMINAL_PHASES: frozenset[SessionPhase] = frozenset(
    {SessionPhase.DONE, SessionPhase.FAILED}
)


def check_transition(current: SessionPhase, requested: SessionPhase) -> None:
    """Поднимает `InvalidTransition`, если ребра нет в графе §2."""
    if requested not in TRANSITIONS[current]:
        raise InvalidTransition(current, requested)
