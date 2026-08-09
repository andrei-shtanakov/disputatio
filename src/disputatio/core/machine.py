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

`apply_decision` материализует `Decision` и проводит FSM по детерминированной
терминальной цепочке §5 [DESIGN-007]/[REQ-013] — каждый hop через
`transition()`, т.е. с тем же write-ahead и событием. `Decision.round` берётся
из `current_round` ДО первого hop'а: revise-петля `CONTINUE` увеличивает
раунд, но решение принадлежит раунду N, не N+1 (I3 [REQ-015]).

Инвариант I4 [DESIGN-008]/[REQ-014]: счётчик повторов текущего шага при
schema-невалидном выводе агента — эфемерный, живёт в `SessionFsm`, а не в
`SessionState` (ADR-W2-05: после kill шаг перезапускается с нуля, что для
I4 безопасно консервативно). `handle_schema_invalid` инкрементирует счётчик
и либо возвращает `RetryAction.RETRY` (без касания портов), либо — при
исчерпании лимита — проводит `transition(FAILED)` и возвращает
`RetryAction.FAILED`. Сброс — явный `handle_step_success()` либо любой
успешный `transition()` (новый шаг — новый лимит).
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum

from disputatio.contracts import (
    Decision,
    Event,
    EventSink,
    EventSource,
    EventType,
    Outcome,
    SessionPhase,
    SessionState,
    StateStore,
)
from disputatio.core.deciding import DecisionDraft
from disputatio.core.transitions import check_transition

# Единственные два ребра, двигающие счётчик раунда: старт сессии открывает
# раунд 1, revise-петля §2 открывает следующий. Все прочие переходы —
# внутри раунда и счётчик не трогают.
_ROUND_START_EDGE = (SessionPhase.IDLE, SessionPhase.PROPOSING)
_REVISE_EDGE = (SessionPhase.DECIDING, SessionPhase.PROPOSING)

# Терминальные цепочки §5 [DESIGN-007]: outcome → hop'ы от DECIDING до
# EXPORTING. CONTINUE не терминален (revise-петля) и в этой карте не
# участвует — за него отвечает отдельная ветка `apply_decision`.
_TERMINAL_CHAINS: Mapping[Outcome, tuple[SessionPhase, ...]] = {
    Outcome.CONVERGED: (SessionPhase.CONVERGED, SessionPhase.EXPORTING),
    Outcome.DEADLOCK: (
        SessionPhase.DEADLOCK,
        SessionPhase.ESCALATED,
        SessionPhase.EXPORTING,
    ),
    Outcome.BUDGET_HIT: (
        SessionPhase.BUDGET_HIT,
        SessionPhase.ESCALATED,
        SessionPhase.EXPORTING,
    ),
}

_PARTIAL_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.DEADLOCK, Outcome.BUDGET_HIT}
)


class RetryAction(StrEnum):
    """Исход `handle_schema_invalid` — инвариант I4 [DESIGN-008]."""

    RETRY = "retry"
    FAILED = "failed"


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
        self._schema_retry_count = 0

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
        self._schema_retry_count = 0  # новый шаг — новый лимит I4
        return new_state

    def handle_schema_invalid(self, detail: str) -> RetryAction:
        """Учёт I4: инкремент счётчика повторов текущего шага [DESIGN-008].

        Пока `count <= limits.schema_retries` — `RetryAction.RETRY`, ни
        состояние, ни порты не тронуты. При исчерпании — `transition(FAILED)`
        (write-ahead + событие) и `RetryAction.FAILED`. `detail` — сообщение
        об ошибке валидации агента (§I4); ядро его не интерпретирует, только
        считает попытки.
        """
        self._schema_retry_count += 1
        if self._schema_retry_count <= self._state.limits.schema_retries:
            return RetryAction.RETRY
        self.transition(SessionPhase.FAILED)
        return RetryAction.FAILED

    def handle_step_success(self) -> None:
        """Сбрасывает счётчик повторов текущего шага (I4 [DESIGN-008])."""
        self._schema_retry_count = 0

    def apply_decision(self, draft: DecisionDraft) -> tuple[Decision, SessionState]:
        """Материализует `Decision` и проходит цепочку переходов §5 [DESIGN-007].

        `CONVERGED`/`DEADLOCK`/`BUDGET_HIT` идут по фиксированной терминальной
        цепочке до `EXPORTING`. `CONTINUE` требует непустую
        `next_round_directive` — иначе `ValueError` без единого hop'а
        (ADR-W2-04): рассинхрон `decision.json`/`session.json` недопустим.
        """
        chain = _TERMINAL_CHAINS.get(draft.outcome)
        if chain is None and not _has_directive(draft.next_round_directive):
            raise ValueError(
                "CONTINUE обязан нести непустую директиву next_round_directive "
                "(ADR-W2-04)"
            )

        decision = Decision(
            round=self._state.current_round,
            outcome=draft.outcome,
            reason=draft.reason,
            open_issues_carried=list(draft.open_issues_carried),
            next_round_directive=draft.next_round_directive,
        )

        if chain is not None:
            for phase in chain:
                self.transition(phase)
            return decision, self._state

        new_state = self.transition(SessionPhase.PROPOSING)
        return decision, new_state


def _has_directive(directive: str | None) -> bool:
    """`True`, если директива непустая и не состоит только из пробелов."""
    return directive is not None and directive.strip() != ""


def is_partial(outcome: Outcome) -> bool:
    """`True` для `DEADLOCK`/`BUDGET_HIT` — частичный результат [DESIGN-007]."""
    return outcome in _PARTIAL_OUTCOMES


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
