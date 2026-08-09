"""FSM сессии — тонкий слой эффектов над графом §2 ([DESIGN-003]).

`SessionFsm` держит текущее `SessionState`, порты и инжектированные часы;
вся логика графа живёт в `transitions.py`, вся недетерминированность — в
`now`. Порядок шагов `transition()` — инвариант I2 (write-ahead):
`session.json` уходит на диск **до** любого сигнала о следующем шаге
[REQ-003], и только после успешного `save` в журнал попадает ровно одно
событие `state_change` [REQ-004].

Ошибки не глотаются. Отказ `StateStore.save` распространяется наружу, и
переход считается несостоявшимся: внутренняя ссылка на состояние не
обновляется, события нет. Отказ `EventSink.emit` (уже после успешного
`save`) тоже распространяется — состояние впереди журнала безопасно для
resume, но молчать о сломанном журнале нельзя; решает вызывающий.
"""

from collections.abc import Callable
from datetime import datetime

from disputatio.contracts import (
    Event,
    EventSink,
    EventSource,
    EventType,
    SessionPhase,
    SessionState,
    StateStore,
)
from disputatio.core.transitions import check_transition

# Единственные два ребра, двигающие счётчик раунда: старт сессии открывает
# раунд 1, revise-петля §2 открывает следующий. Все прочие переходы —
# внутри раунда и счётчик не трогают.
_ROUND_START_EDGE = (SessionPhase.IDLE, SessionPhase.PROPOSING)
_REVISE_EDGE = (SessionPhase.DECIDING, SessionPhase.PROPOSING)


class SessionFsm:
    """FSM одной сессии; все эффекты — через ports (I2 write-ahead)."""

    def __init__(
        self,
        state: SessionState,
        *,
        store: StateStore,
        sink: EventSink,
        now: Callable[[], datetime],
    ) -> None:
        """Связывает начальное состояние с портами и инжектированными часами."""
        self._state = state
        self._store = store
        self._sink = sink
        self._now = now

    @property
    def state(self) -> SessionState:
        """Текущее состояние сессии; обновляется только успешным переходом."""
        return self._state

    def transition(self, to: SessionPhase) -> SessionState:
        """check → model_copy → store.save (write-ahead) → sink.emit.

        Возвращает новое `SessionState` — сигнал «шаг можно начинать».
        Поднимает `InvalidTransition`, если ребра нет в графе §2; в этом
        случае ни состояние, ни хранилище, ни журнал не тронуты.
        """
        current = self._state.state
        check_transition(current, to)

        new_state = self._state.model_copy(
            update={
                "state": to,
                "current_round": _next_round(current, to, self._state.current_round),
            }
        )
        self._store.save(new_state)  # write-ahead I2: до события и до шага
        self._sink.emit(
            Event(
                ts=self._now(),
                session=new_state.session_id,
                # `Event.round` требует ge=1: раунд 0 — это «сессия ещё не
                # начиналась», такому событию раунда не полагается.
                round=new_state.current_round or None,
                source=EventSource.ORCHESTRATOR,
                type=EventType.STATE_CHANGE,
                payload={"from": current.value, "to": to.value},
            )
        )
        self._state = new_state
        return new_state


def _next_round(
    current: SessionPhase, requested: SessionPhase, current_round: int
) -> int:
    """Счётчик раунда после перехода `current → requested`."""
    edge = (current, requested)
    if edge == _ROUND_START_EDGE:
        return 1
    if edge == _REVISE_EDGE:
        return current_round + 1
    return current_round
