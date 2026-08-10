"""Тесты TASK-016: контракты ports и `AgentTurn.tokens_used` ([REQ-016]).

`tokens_used is None` означает «адаптер не сообщил расход» — «неизвестно»
обязано отличаться от явного `0`, иначе учёт бюджета BUDGET_HIT (§5.2)
слепнет на адаптерах без token-метрик. Докстринги контрактов (`KeyError`
у `StateStore.load`, место §8-стриминга у `EventSink`) проверяются по
подстроке — дешёвый гард от отката докстрингов ([DESIGN-014]). Async-фейк
через anyio перепокрывает сценарий superseded-теста
`test_ports.py::test_agent_adapter_fake_runs_via_anyio` (ADR-006).
"""

import anyio

from disputatio.contracts import ports
from disputatio.contracts.ports import AgentAdapter, AgentTurn


class FakeMetriclessAgentAdapter:
    """Async-фейк AgentAdapter без token-метрик: tokens_used не задаёт."""

    async def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
        return AgentTurn(text=f"echo: {prompt}", session_ref=session_ref)


def test_agent_turn_tokens_used_defaults_to_none() -> None:
    """AgentTurn без tokens_used → None («не сообщил»), а не 0."""
    turn = AgentTurn(text="сырой вывод агента")
    assert turn.tokens_used is None


def test_agent_turn_accepts_explicit_zero_and_positive_tokens() -> None:
    """Явный 0 («сообщил: ноль») и положительные значения принимаются."""
    assert AgentTurn(text="t", tokens_used=0).tokens_used == 0
    assert AgentTurn(text="t", tokens_used=12_345).tokens_used == 12_345


def test_agent_adapter_fake_runs_via_anyio_without_metrics() -> None:
    """Async-фейк типизируется портом; без метрик tokens_used is None.

    Перепокрытие superseded-теста `test_agent_adapter_fake_runs_via_anyio`
    (ADR-006): echo-семантика, проброс session_ref и anyio-запуск сохранены.
    """
    adapter: AgentAdapter = FakeMetriclessAgentAdapter()

    async def call() -> AgentTurn:
        return await adapter.run("ping", session_ref="ref-1")

    turn = anyio.run(call)
    assert isinstance(turn, AgentTurn)
    assert turn.text == "echo: ping"
    assert turn.session_ref == "ref-1"
    assert turn.tokens_used is None


def test_state_store_load_docstring_documents_key_error() -> None:
    """Докстринг StateStore.load фиксирует KeyError отсутствующей сессии."""
    doc = ports.StateStore.load.__doc__
    assert doc is not None
    assert "KeyError" in doc


def test_module_docstring_documents_event_sink_injection() -> None:
    """Докстринг модуля: §8-стриминг — EventSink инжектится в адаптер."""
    doc = ports.__doc__
    assert doc is not None
    assert "EventSink инжектится" in doc
