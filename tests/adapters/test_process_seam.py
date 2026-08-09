"""Subprocess seam + минимальный `ClaudeCodeAdapter.run()` — TASK-003.

[DESIGN-002], [REQ-003], [REQ-012]. Тест конструирует `ClaudeCodeAdapter` с
фейковым `launcher`, возвращающим `make_fake_process(...)`, и проверяет, что
`run()` собирает `AgentTurn` из построчного `stdout` фейка, не трогая
`asyncio.create_subprocess_exec`.
"""

import asyncio
from pathlib import Path

import anyio
import pytest

from disputatio.contracts.base import Role
from disputatio.contracts.ports import AgentAdapter, AgentTurn


def test_run_returns_agent_turn_from_fake_stdout(
    tmp_path: Path, make_fake_process
) -> None:
    from disputatio.adapters.claude_code import ClaudeCodeAdapter

    try:
        adapter = ClaudeCodeAdapter(
            role=Role.AUTHOR,
            session_dir=tmp_path,
            launcher=lambda *a, **kw: make_fake_process(["hello", "world"]),
        )
        turn = anyio.run(adapter.run, "do the thing")
    except (TypeError, NotImplementedError) as exc:
        raise AssertionError(
            "ClaudeCodeAdapter ещё не поддерживает launcher/run() из TASK-003"
        ) from exc

    assert isinstance(turn, AgentTurn)
    assert turn.text == "hello\nworld\n"
    assert turn.session_ref is None
    assert turn.tokens_used is None


def test_run_never_touches_real_subprocess_exec(
    tmp_path: Path, make_fake_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    from disputatio.adapters.claude_code import ClaudeCodeAdapter

    def _raise(*args: object, **kwargs: object) -> None:
        raise AssertionError("run() дотронулся до настоящего create_subprocess_exec")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)

    try:
        adapter = ClaudeCodeAdapter(
            role=Role.AUTHOR,
            session_dir=tmp_path,
            launcher=lambda *a, **kw: make_fake_process(["ok"]),
        )
        turn = anyio.run(adapter.run, "prompt")
    except (TypeError, NotImplementedError) as exc:
        raise AssertionError(
            "ClaudeCodeAdapter ещё не поддерживает launcher/run() из TASK-003"
        ) from exc

    assert turn.text == "ok\n"


def test_claude_code_adapter_satisfies_agent_adapter_protocol(tmp_path: Path) -> None:
    from disputatio.adapters.claude_code import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter(role=Role.AUTHOR, session_dir=tmp_path)

    assert isinstance(adapter, AgentAdapter)
