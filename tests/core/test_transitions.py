"""Тесты графа переходов §2: TASK-001, [DESIGN-001].

Импорты `disputatio.core.transitions` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор (`test_full_graph_is_pinned`) превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
"""

from itertools import pairwise

import pytest

from disputatio.contracts import SessionPhase

P = SessionPhase

# Полный граф §2 по таблице DESIGN-001 — эталон для пина и производных
# проверок (терминальность, →FAILED из пяти активных состояний, цепочки).
EXPECTED_GRAPH: dict[SessionPhase, set[SessionPhase]] = {
    P.IDLE: {P.PROPOSING},
    P.PROPOSING: {P.VERIFYING, P.FAILED},
    P.VERIFYING: {P.REVIEWING, P.FAILED},
    P.REVIEWING: {P.DECIDING, P.FAILED},
    P.DECIDING: {P.PROPOSING, P.CONVERGED, P.DEADLOCK, P.BUDGET_HIT, P.FAILED},
    P.CONVERGED: {P.EXPORTING},
    P.DEADLOCK: {P.ESCALATED},
    P.BUDGET_HIT: {P.ESCALATED},
    P.ESCALATED: {P.EXPORTING},
    P.EXPORTING: {P.DONE, P.FAILED},
    P.DONE: set(),
    P.FAILED: set(),
}

ACTIVE_PHASES_WITH_FAILED_EDGE = (
    P.PROPOSING,
    P.VERIFYING,
    P.REVIEWING,
    P.DECIDING,
    P.EXPORTING,
)


def test_full_graph_is_pinned() -> None:
    """Снимок всех рёбер `TRANSITIONS` совпадает с таблицей DESIGN-001."""
    try:
        from disputatio.core.transitions import TRANSITIONS
    except ImportError as exc:  # red-фаза: transitions.py ещё не создан
        raise AssertionError(
            "src/disputatio/core/transitions.py ещё не создан"
        ) from exc

    snapshot = {phase: set(targets) for phase, targets in TRANSITIONS.items()}
    assert snapshot == EXPECTED_GRAPH


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (P.IDLE, P.PROPOSING),
        (P.DECIDING, P.PROPOSING),  # revise-петля
        (P.VERIFYING, P.REVIEWING),  # независимо от результата verification
    ],
)
def test_allowed_edges_pass_check(
    current: SessionPhase, requested: SessionPhase
) -> None:
    """Разрешённые рёбра проходят `check_transition` без исключения."""
    from disputatio.core.transitions import check_transition

    assert check_transition(current, requested) is None


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (P.IDLE, P.REVIEWING),
        (P.DONE, P.PROPOSING),
        (P.REVIEWING, P.VERIFYING),  # возвратов внутри раунда нет (I3)
    ],
)
def test_forbidden_edges_raise_invalid_transition(
    current: SessionPhase, requested: SessionPhase
) -> None:
    """Запрещённые рёбра отклоняются типизированной `InvalidTransition`."""
    from disputatio.core.transitions import InvalidTransition, check_transition

    with pytest.raises(InvalidTransition):
        check_transition(current, requested)


def test_terminal_phases_have_no_outgoing_edges() -> None:
    """`DONE`/`FAILED` терминальны: пустое множество исходящих рёбер."""
    from disputatio.core.transitions import TERMINAL_PHASES, TRANSITIONS

    assert TERMINAL_PHASES == frozenset({P.DONE, P.FAILED})
    for phase in TERMINAL_PHASES:
        assert TRANSITIONS[phase] == frozenset()


def test_transitions_total_over_session_phase() -> None:
    """`TRANSITIONS` тотально по всем 12 состояниям enum `SessionPhase`."""
    from disputatio.core.transitions import TRANSITIONS

    assert set(TRANSITIONS.keys()) == set(SessionPhase)
    for phase, targets in TRANSITIONS.items():
        assert isinstance(targets, frozenset), phase
        assert all(isinstance(target, SessionPhase) for target in targets), phase


def test_invalid_transition_carries_current_and_requested() -> None:
    """`InvalidTransition` несёт оба состояния: `current` и `requested`."""
    from disputatio.core.transitions import InvalidTransition, check_transition

    with pytest.raises(InvalidTransition) as excinfo:
        check_transition(P.IDLE, P.REVIEWING)

    assert excinfo.value.current is P.IDLE
    assert excinfo.value.requested is P.REVIEWING


def test_check_transition_does_not_mutate_graph() -> None:
    """Вызовы `check_transition` (успех и отказ) не меняют `TRANSITIONS`."""
    from disputatio.core.transitions import (
        TRANSITIONS,
        InvalidTransition,
        check_transition,
    )

    before = {phase: set(targets) for phase, targets in TRANSITIONS.items()}
    check_transition(P.IDLE, P.PROPOSING)
    with pytest.raises(InvalidTransition):
        check_transition(P.DONE, P.PROPOSING)
    after = {phase: set(targets) for phase, targets in TRANSITIONS.items()}

    assert after == before


def test_failed_reachable_from_five_active_phases() -> None:
    """ADR-W2-01: `FAILED` достижим из каждого из пяти активных состояний."""
    from disputatio.core.transitions import TRANSITIONS, check_transition

    for phase in ACTIVE_PHASES_WITH_FAILED_EDGE:
        assert P.FAILED in TRANSITIONS[phase], phase
        assert check_transition(phase, P.FAILED) is None


@pytest.mark.parametrize(
    "chain",
    [
        (P.CONVERGED, P.EXPORTING, P.DONE),
        (P.DEADLOCK, P.ESCALATED, P.EXPORTING, P.DONE),
        (P.BUDGET_HIT, P.ESCALATED, P.EXPORTING, P.DONE),
    ],
)
def test_terminal_chains_pass_edge_by_edge(chain: tuple[SessionPhase, ...]) -> None:
    """Цепочки `CONVERGED→…` и `DEADLOCK/BUDGET_HIT→…→DONE` проходят по рёбрам."""
    from disputatio.core.transitions import check_transition

    for current, requested in pairwise(chain):
        assert check_transition(current, requested) is None
