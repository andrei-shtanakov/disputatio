"""Тесты `SessionFsm.transition`: TASK-004, [DESIGN-003], [REQ-003], [REQ-004].

Проверяется тонкий слой эффектов: write-ahead (I2) — `StateStore.save`
строго раньше `EventSink.emit`, ровно одно событие `state_change` на
переход, несостоявшийся переход не оставляет следов, и счётчик раунда
двигается только на двух рёбрах (`IDLE→PROPOSING`, revise-петля
`DECIDING→PROPOSING`).

Импорт `disputatio.core.machine` выполняется внутри `_make_fsm`: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы
collection. Хелпер превращает `ImportError` в `AssertionError` — гейт
принимает red только при падении assertion'ом.
"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from disputatio.contracts import (
    EventSource,
    EventType,
    SessionPhase,
    SessionState,
)
from disputatio.core.transitions import InvalidTransition

from .conftest import (
    FIXED_NOW,
    FailingStateStore,
    FakeEventSink,
    FakeStateStore,
    StoreFailure,
    make_session_state,
)

P = SessionPhase

# Рёбра, на которых раунд обязан остаться прежним ([DESIGN-003]): двигают
# счётчик только старт сессии и revise-петля.
ROUND_PRESERVING_EDGES = (
    (P.PROPOSING, P.VERIFYING),
    (P.VERIFYING, P.REVIEWING),
    (P.REVIEWING, P.DECIDING),
    (P.DECIDING, P.CONVERGED),
    (P.CONVERGED, P.EXPORTING),
    (P.EXPORTING, P.DONE),
    (P.DECIDING, P.FAILED),
)


def _make_fsm(
    state: SessionState,
    *,
    store: Any,
    sink: Any,
    now: Callable[[], datetime],
) -> Any:
    """Строит `SessionFsm`; отсутствие модуля — assertion, не ImportError."""
    try:
        from disputatio.core.machine import SessionFsm
    except ImportError as exc:  # red-фаза: machine.py ещё не создан
        raise AssertionError("src/disputatio/core/machine.py ещё не создан") from exc

    return SessionFsm(state, store=store, sink=sink, now=now)


def test_save_precedes_emit_on_transition(
    calls: list[str],
    store: FakeStateStore,
    sink: FakeEventSink,
    fixed_now: Callable[[], datetime],
) -> None:
    """Write-ahead I2: `save` нового состояния строго раньше `emit`."""
    state = make_session_state(state=P.PROPOSING, current_round=1)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    fsm.transition(P.VERIFYING)

    assert calls == ["save(phase=VERIFYING)", "emit(state_change)"]


def test_failing_store_aborts_transition(
    calls: list[str],
    failing_store: FailingStateStore,
    sink: FakeEventSink,
    fixed_now: Callable[[], datetime],
) -> None:
    """Отказ `save` — переход несостоявшийся: ни состояния, ни события."""
    state = make_session_state(state=P.PROPOSING, current_round=1)
    fsm = _make_fsm(state, store=failing_store, sink=sink, now=fixed_now)

    with pytest.raises(StoreFailure):
        fsm.transition(P.VERIFYING)

    assert fsm.state == state
    assert sink.events == []
    assert calls == ["save_failed(phase=VERIFYING)"]


def test_state_change_event_is_single_and_pinned(
    store: FakeStateStore,
    sink: FakeEventSink,
    fixed_now: Callable[[], datetime],
) -> None:
    """Успешный переход в раунде N даёт ровно одно запинённое событие."""
    state = make_session_state(state=P.VERIFYING, current_round=3)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    new_state = fsm.transition(P.REVIEWING)

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.type is EventType.STATE_CHANGE
    assert event.source is EventSource.ORCHESTRATOR
    assert event.session == state.session_id
    assert event.round == 3
    assert event.ts == FIXED_NOW
    assert event.payload == {"from": "VERIFYING", "to": "REVIEWING"}
    # events.jsonl — append-only JSON: payload обязан пережить сериализацию.
    assert json.loads(json.dumps(event.payload)) == {
        "from": "VERIFYING",
        "to": "REVIEWING",
    }
    assert new_state.state is P.REVIEWING
    assert fsm.state == new_state


def test_invalid_transition_touches_nothing(
    calls: list[str],
    store: FakeStateStore,
    sink: FakeEventSink,
    fixed_now: Callable[[], datetime],
) -> None:
    """Ребра нет в графе §2: состояние, `save` и журнал не тронуты."""
    state = make_session_state(state=P.IDLE, current_round=0)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    with pytest.raises(InvalidTransition) as exc_info:
        fsm.transition(P.REVIEWING)

    assert exc_info.value.current is P.IDLE
    assert exc_info.value.requested is P.REVIEWING
    assert fsm.state == state
    assert store.saved == []
    assert sink.events == []
    assert calls == []


def test_idle_to_proposing_starts_first_round(
    store: FakeStateStore,
    sink: FakeEventSink,
    fixed_now: Callable[[], datetime],
) -> None:
    """`IDLE→PROPOSING` открывает раунд 1 — в состоянии и в событии."""
    state = make_session_state(state=P.IDLE, current_round=0)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    new_state = fsm.transition(P.PROPOSING)

    assert new_state.current_round == 1
    assert fsm.state.current_round == 1
    assert store.saved[-1].current_round == 1
    assert sink.events[0].round == 1


def test_revise_loop_increments_round(
    store: FakeStateStore,
    sink: FakeEventSink,
    fixed_now: Callable[[], datetime],
) -> None:
    """Revise-петля `DECIDING→PROPOSING` увеличивает раунд на единицу."""
    state = make_session_state(state=P.DECIDING, current_round=2)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    new_state = fsm.transition(P.PROPOSING)

    assert new_state.current_round == 3
    assert store.saved[-1].current_round == 3
    assert sink.events[0].round == 3


@pytest.mark.parametrize(("current", "requested"), ROUND_PRESERVING_EDGES)
def test_other_transitions_keep_round(
    current: SessionPhase,
    requested: SessionPhase,
    store: FakeStateStore,
    sink: FakeEventSink,
    fixed_now: Callable[[], datetime],
) -> None:
    """Все прочие рёбра меняют только фазу: раунд остаётся прежним."""
    state = make_session_state(state=current, current_round=4)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    new_state = fsm.transition(requested)

    assert new_state.current_round == 4
    assert new_state.state is requested
    assert store.saved[-1].current_round == 4
    assert sink.events[0].round == 4
