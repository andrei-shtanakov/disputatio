"""Тесты пакета `disputatio.adapters` — TASK-002, [DESIGN-001], [REQ-002].

`import disputatio.adapters` идёт через `importlib.import_module`, а не
через top-level `import`: на red-чекпоинте пакета ещё не существует, и
top-level `import` дал бы collection-error (не `AssertionError`), который
red-гейт не засчитывает (см. аналогичный паттерн в `test_conftest_fakes.py`).
"""

import asyncio
import importlib
import os
from pathlib import Path

import pytest


def _raise(*args: object, **kwargs: object) -> None:
    raise AssertionError("побочный эффект при импорте disputatio.adapters")


def test_import_disputatio_adapters_has_no_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)
    monkeypatch.setattr(os, "mkdir", _raise)

    try:
        module = importlib.import_module("disputatio.adapters")
    except ModuleNotFoundError as exc:
        raise AssertionError("пакет disputatio.adapters ещё не создан") from exc

    assert module.ClaudeCodeAdapter is not None
    assert module.CodexAdapter is not None
    assert module.AdapterCapabilities is not None


def test_public_names_reexported_via_all() -> None:
    adapters = importlib.import_module("disputatio.adapters")

    assert adapters.__all__ == [
        "ClaudeCodeAdapter",
        "CodexAdapter",
        "AdapterCapabilities",
    ]
    for name in adapters.__all__:
        assert hasattr(adapters, name)


def test_adapter_stubs_satisfy_agent_adapter_protocol(tmp_path: Path) -> None:
    from disputatio.adapters import ClaudeCodeAdapter, CodexAdapter
    from disputatio.contracts.base import Role
    from disputatio.contracts.ports import AgentAdapter

    claude_code = ClaudeCodeAdapter(role=Role.AUTHOR, session_dir=tmp_path)
    codex = CodexAdapter(role=Role.AUTHOR, session_dir=tmp_path)

    assert isinstance(claude_code, AgentAdapter)
    assert isinstance(codex, AgentAdapter)
