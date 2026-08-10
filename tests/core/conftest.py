"""Фейки ports и фикстуры для `tests/core/**` ([DESIGN-009], [REQ-005]).

Ядро FSM выражает всю зависимость от среды через порты contracts, поэтому
его тесты работают на in-memory фейках: ни диска, ни сети, ни sleep, ни
недетерминированных часов ([NFR-003]).

Ключевая деталь — **общий журнал вызовов** `calls: list[str]`: `FakeStateStore`
и `FakeEventSink` дописывают в один и тот же список, поэтому тест может
пинать не только факт вызова, но и порядок «save строго раньше emit»
(write-ahead I2, [REQ-003]/[REQ-004]). Формат записей стабилен и является
частью контракта фейков:

* ``save(phase=VERIFYING)``      — успешное сохранение состояния;
* ``save_failed(phase=VERIFYING)`` — отказ `FailingStateStore.save`;
* ``load(<session_id>)``         — чтение состояния;
* ``emit(state_change)``         — событие в журнал.

Часы инжектируются фикстурой `fixed_now` (`Callable[[], datetime]`): ядру
запрещено звать `datetime.now`, а тесты обязаны быть детерминированными.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from disputatio.contracts import (
    AgentRef,
    BudgetUsed,
    Event,
    EventSink,
    Limits,
    Mode,
    Role,
    SessionPhase,
    SessionState,
    StateStore,
    TaskSpec,
)

# Константа инжектированных часов: одно значение на весь прогон, чтобы
# `ts` событий и `created_at` состояний были воспроизводимы побайтово.
FIXED_NOW: datetime = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class StoreFailure(RuntimeError):
    """Отказ хранилища, имитируемый `FailingStateStore.save`."""


class FakeStateStore:
    """In-memory `StateStore`: состояния в словаре, вызовы — в общий журнал.

    `calls` можно передать снаружи (общий журнал с `FakeEventSink`); без
    аргумента фейк заводит собственный пустой список.
    """

    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls: list[str] = [] if calls is None else calls
        self.saved: list[SessionState] = []
        self._states: dict[str, SessionState] = {}

    def load(self, session_id: str) -> SessionState:
        """Возвращает последнее сохранённое состояние; иначе `KeyError`."""
        self.calls.append(f"load({session_id})")
        return self._states[session_id]

    def save(self, state: SessionState) -> None:
        """Сохраняет состояние в памяти и журналирует фазу записи."""
        self.calls.append(f"save(phase={state.state.value})")
        self.saved.append(state)
        self._states[state.session_id] = state


class FailingStateStore(FakeStateStore):
    """`StateStore`, чей `save` всегда бросает `StoreFailure`.

    Журналирует попытку как ``save_failed(phase=…)``: тест отличает
    «сохранения не было» от «сохранение прошло», не заглядывая внутрь.
    """

    def save(self, state: SessionState) -> None:
        """Журналирует попытку и бросает `StoreFailure`; ничего не хранит."""
        self.calls.append(f"save_failed(phase={state.state.value})")
        raise StoreFailure(f"фейк хранилища отказал на save({state.session_id})")


class FakeEventSink:
    """In-memory `EventSink`: копит события и пишет в общий журнал."""

    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls: list[str] = [] if calls is None else calls
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        """Дописывает событие в память и журналирует его тип."""
        self.calls.append(f"emit({event.type.value})")
        self.events.append(event)


if TYPE_CHECKING:
    # ADR-004: `isinstance` у runtime_checkable-протокола проверяет только
    # наличие методов; совпадение сигнатур пинает pyrefly на присваивании
    # фейка переменной типа порта.
    _state_store_port: StateStore = FakeStateStore()
    _failing_store_port: StateStore = FailingStateStore()
    _event_sink_port: EventSink = FakeEventSink()


def make_session_state(
    *,
    session_id: str = "sess-20260809-01",
    state: SessionPhase = SessionPhase.IDLE,
    current_round: int = 0,
    mode: Mode = Mode.DEVELOP,
    max_rounds: int = 8,
) -> SessionState:
    """Минимальный валидный `SessionState` — груз для фейков хранилища."""
    return SessionState(
        session_id=session_id,
        created_at=FIXED_NOW,
        state=state,
        current_round=current_round,
        task=TaskSpec(prompt="починить парсер", mode=mode),
        agents={
            Role.AUTHOR: AgentRef(adapter="claude_code", model="claude-sonnet-5"),
            Role.REVIEWER: AgentRef(adapter="codex", model="gpt-5.4"),
        },
        limits=Limits(
            max_rounds=max_rounds,
            max_total_tokens=1_000_000,
            max_wall_seconds=3600,
            schema_retries=2,
        ),
        budget_used=BudgetUsed(),
    )


@pytest.fixture
def calls() -> list[str]:
    """Общий журнал вызовов фейков — свежий список на каждый тест."""
    return []


@pytest.fixture
def store(calls: list[str]) -> FakeStateStore:
    """`FakeStateStore`, пишущий в общий журнал `calls`."""
    return FakeStateStore(calls)


@pytest.fixture
def failing_store(calls: list[str]) -> FailingStateStore:
    """`FailingStateStore`, пишущий в общий журнал `calls`."""
    return FailingStateStore(calls)


@pytest.fixture
def sink(calls: list[str]) -> FakeEventSink:
    """`FakeEventSink`, пишущий в общий журнал `calls`."""
    return FakeEventSink(calls)


@pytest.fixture
def fixed_now() -> Callable[[], datetime]:
    """Инжектированные часы: любой вызов возвращает `FIXED_NOW`."""

    def _now() -> datetime:
        return FIXED_NOW

    return _now


@pytest.fixture
def session_state() -> SessionState:
    """Минимальный `SessionState` в фазе `IDLE`, раунд 0."""
    return make_session_state()
