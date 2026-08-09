"""Тесты ports.py — Protocol-порты и AgentTurn: TASK-011, [DESIGN-009], [REQ-012].

Импорты `disputatio.contracts.ports` и `anyio` выполняются внутри тестов: на
момент red-чекпоинта модуля ещё нет (а anyio не установлен), и импорт на
уровне модуля сломал бы collection. Red-селектор
(`test_fakes_pass_isinstance_checks`) превращает ImportError в AssertionError —
гейт принимает red только при падении assertion'ом. Фейки — структурные, без
наследования от Protocol (ADR-004): `isinstance` проверяет наличие методов,
сигнатуры ловит pyrefly через присваивание переменной с типом Protocol.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from disputatio.contracts.base import Role
from disputatio.contracts.events import Event
from disputatio.contracts.session import (
    AgentRef,
    BudgetUsed,
    Limits,
    Mode,
    SessionPhase,
    SessionState,
    TaskSpec,
)
from disputatio.contracts.verification import (
    DiffStats,
    OverallStatus,
    VerificationReport,
)

if TYPE_CHECKING:
    from disputatio.contracts.ports import AgentTurn


def make_session_state() -> SessionState:
    """Минимальный валидный SessionState — груз для фейка StateStore."""
    return SessionState(
        session_id="sess-20260808-01",
        created_at=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
        state=SessionPhase.IDLE,
        current_round=0,
        task=TaskSpec(prompt="починить парсер", mode=Mode.DEVELOP),
        agents={
            Role.AUTHOR: AgentRef(adapter="claude_code", model="claude-sonnet-5"),
            Role.REVIEWER: AgentRef(adapter="codex", model="gpt-5.4"),
        },
        limits=Limits(
            max_rounds=8,
            max_total_tokens=1_000_000,
            max_wall_seconds=3600,
            schema_retries=2,
        ),
        budget_used=BudgetUsed(),
    )


def make_event() -> Event:
    """Минимальное валидное событие — груз для фейка EventSink."""
    return Event.model_validate(
        {
            "schema": "disputatio/v1",
            "ts": "2026-08-08T12:00:00+00:00",
            "session": "sess-20260808-01",
            "round": 3,
            "source": "orchestrator",
            "type": "state_change",
            "payload": {},
        }
    )


def make_verification_report(round_no: int) -> VerificationReport:
    """Минимальный валидный VerificationReport — ответ фейка Verifier."""
    return VerificationReport(
        round=round_no,
        gates=[],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=0, insertions=0, deletions=0),
    )


class FakeStateStore:
    """Sync-фейк порта StateStore: хранит состояния в памяти."""

    def __init__(self) -> None:
        self._states: dict[str, SessionState] = {}

    def load(self, session_id: str) -> SessionState:
        return self._states[session_id]

    def save(self, state: SessionState) -> None:
        self._states[state.session_id] = state


class FakeEventSink:
    """Sync-фейк порта EventSink: копит события в списке."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class FakeAgentAdapter:
    """Async-фейк порта AgentAdapter: echo-семантика без subprocess."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> "AgentTurn":
        from disputatio.contracts.ports import AgentTurn

        return AgentTurn(text=f"echo: {prompt}", session_ref=session_ref)


class FakeVerifier:
    """Sync-фейк порта Verifier: отчёт с overall=pass без гейтов."""

    def verify(self, round_no: int) -> VerificationReport:
        return make_verification_report(round_no)


def test_fakes_pass_isinstance_checks() -> None:
    """Структурный фейк каждого порта проходит isinstance-проверку."""
    try:
        from disputatio.contracts import ports
    except ImportError as exc:  # red-фаза: ports.py ещё не создан
        raise AssertionError("src/disputatio/contracts/ports.py ещё не создан") from exc

    assert isinstance(FakeStateStore(), ports.StateStore)
    assert isinstance(FakeEventSink(), ports.EventSink)
    assert isinstance(FakeAgentAdapter(), ports.AgentAdapter)
    assert isinstance(FakeVerifier(), ports.Verifier)


def test_object_without_methods_fails_every_protocol() -> None:
    """Объект вовсе без методов не проходит ни один из четырёх портов."""
    from disputatio.contracts import ports

    class Empty:
        pass

    protocols = (ports.StateStore, ports.EventSink, ports.AgentAdapter, ports.Verifier)
    for protocol in protocols:
        assert not isinstance(Empty(), protocol)


def test_state_store_requires_both_methods() -> None:
    """Фейк с load, но без save — не StateStore: нужны оба метода."""
    from disputatio.contracts import ports

    class LoadOnly:
        def load(self, session_id: str) -> SessionState:
            return make_session_state()

    assert not isinstance(LoadOnly(), ports.StateStore)


def test_sync_fakes_satisfy_protocol_annotations() -> None:
    """Sync-фейки присваиваются переменным с типом Protocol (ловит pyrefly)."""
    from disputatio.contracts.ports import EventSink, StateStore, Verifier

    store: StateStore = FakeStateStore()
    state = make_session_state()
    store.save(state)
    assert store.load(state.session_id) == state

    fake_sink = FakeEventSink()
    sink: EventSink = fake_sink
    event = make_event()
    sink.emit(event)
    assert fake_sink.events == [event]

    verifier: Verifier = FakeVerifier()
    report = verifier.verify(3)
    assert report.round == 3
    assert report.overall == OverallStatus.PASS


def test_agent_adapter_fake_runs_via_anyio() -> None:
    """Async-фейк AgentAdapter типизируется портом и вызывается через anyio."""
    import anyio

    from disputatio.contracts.ports import AgentAdapter, AgentTurn

    adapter: AgentAdapter = FakeAgentAdapter()

    async def call() -> AgentTurn:
        return await adapter.run("ping", session_ref="ref-1")

    turn = anyio.run(call)
    assert isinstance(turn, AgentTurn)
    assert turn.text == "echo: ping"
    assert turn.session_ref == "ref-1"
    assert turn.tokens_used == 0


def test_agent_turn_defaults() -> None:
    """AgentTurn: обязателен только text; session_ref=None, tokens_used=0."""
    from disputatio.contracts.ports import AgentTurn

    turn = AgentTurn(text="сырой вывод агента")
    assert turn.text == "сырой вывод агента"
    assert turn.session_ref is None
    assert turn.tokens_used == 0


def test_agent_turn_requires_text() -> None:
    """Без text AgentTurn не валидируется."""
    from disputatio.contracts.ports import AgentTurn

    with pytest.raises(ValidationError):
        AgentTurn.model_validate({})
