"""Review-fix TASK-004: атомарность `FileStateStore.save` ([REQ-004], [REQ-001]).

`tests/events/test_state_store.py` байт-залочен red-чекпоинтом TASK-004, а
критерий приёмки REQ-004 «session.json перезаписывается атомарно (temp-file +
rename)» им не запинен: подмена `atomic_write` на `Path.write_text` проходит
весь suite. Эти регрессии закрывают пробел в отдельном файле, не трогая
залоченный.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from disputatio.contracts.base import Role
from disputatio.contracts.session import (
    AgentRef,
    BudgetUsed,
    Limits,
    Mode,
    SessionPhase,
    SessionState,
    TaskSpec,
)
from disputatio.events import atomic
from disputatio.events.bootstrap import bootstrap_session
from disputatio.events.paths import session_dir, session_json_path
from disputatio.events.state_store import FileStateStore


def make_state(current_round: int = 1) -> SessionState:
    """Минимальный валидный `SessionState`, различимый по `current_round`."""
    return SessionState(
        session_id="sess-1",
        created_at=datetime(2026, 8, 9, 12, 30, tzinfo=UTC),
        state=SessionPhase.PROPOSING,
        current_round=current_round,
        task=TaskSpec(prompt="починить парсер", mode=Mode.DEVELOP),
        agents={
            Role.AUTHOR: AgentRef(adapter="claude_code", model="claude-opus-5"),
            Role.REVIEWER: AgentRef(adapter="codex", model="gpt-5"),
        },
        limits=Limits(
            max_rounds=8,
            max_total_tokens=1_000_000,
            max_wall_seconds=3600,
            schema_retries=2,
        ),
        budget_used=BudgetUsed(tokens=0),
    )


def test_save_failure_at_rename_leaves_previous_state_intact(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой на `os.replace` не должен разрушать уже сохранённое состояние."""
    bootstrap_session(session_root)
    store = FileStateStore(session_root)
    store.save(make_state(current_round=1))

    monkeypatch.setattr(atomic.os, "replace", Mock(side_effect=OSError("boom")))

    with pytest.raises(OSError):
        store.save(make_state(current_round=2))

    assert store.load("sess-1").current_round == 1


def test_save_replaces_session_json_by_rename_without_leftovers(
    session_root: Path,
) -> None:
    """Успешный `save` подменяет файл целиком (новый inode) и не сорит `.tmp`."""
    bootstrap_session(session_root)
    store = FileStateStore(session_root)
    store.save(make_state(current_round=1))
    inode_before = session_json_path(session_root).stat().st_ino

    store.save(make_state(current_round=2))

    assert session_json_path(session_root).stat().st_ino != inode_before
    assert list(session_dir(session_root).glob("*.tmp*")) == []
