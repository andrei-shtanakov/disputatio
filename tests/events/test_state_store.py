"""Тесты `state_store.py`: TASK-004, [DESIGN-003], [REQ-004], [REQ-005], [REQ-011].

Импорт `disputatio.events.state_store` внутри тестов: на момент red-чекпоинта
модуля ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_save_then_load_roundtrip`) превращает `ImportError` в `AssertionError` —
гейт принимает red только при падении assertion'ом.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from disputatio.contracts import ports
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
from disputatio.events.bootstrap import bootstrap_session
from disputatio.events.paths import session_json_path


def make_state(
    *,
    session_id: str = "sess-1",
    current_round: int = 1,
    state: SessionPhase = SessionPhase.PROPOSING,
    prompt: str = "починить парсер",
    mode: Mode = Mode.DEVELOP,
    tokens: int = 0,
) -> SessionState:
    """Валидный `SessionState` с обоими агентами (§4.1 SPEC-001)."""
    return SessionState(
        session_id=session_id,
        created_at=datetime(2026, 8, 9, 12, 30, tzinfo=UTC),
        state=state,
        current_round=current_round,
        task=TaskSpec(prompt=prompt, mode=mode),
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
        budget_used=BudgetUsed(tokens=tokens),
    )


def test_save_then_load_roundtrip(session_root: Path) -> None:
    try:
        from disputatio.events.state_store import FileStateStore
    except ImportError as exc:  # red-фаза: state_store.py ещё не создан
        raise AssertionError(
            "src/disputatio/events/state_store.py ещё не создан"
        ) from exc

    bootstrap_session(session_root)
    store = FileStateStore(session_root)
    state = make_state()

    store.save(state)
    loaded = store.load(state.session_id)

    assert loaded == state
    assert loaded.model_dump(mode="json", by_alias=True) == state.model_dump(
        mode="json", by_alias=True
    )


def test_load_missing_session_raises_keyerror(session_root: Path) -> None:
    from disputatio.events.state_store import FileStateStore

    bootstrap_session(session_root)
    store = FileStateStore(session_root)

    with pytest.raises(KeyError) as excinfo:
        store.load("nonexistent")

    assert excinfo.value.args == ("nonexistent",)


def test_load_session_id_mismatch_raises_keyerror(session_root: Path) -> None:
    """ADR-006: чужой `session_id` в файле — «сессии нет», а не чужое состояние."""
    from disputatio.events.state_store import FileStateStore

    bootstrap_session(session_root)
    store = FileStateStore(session_root)
    store.save(make_state(session_id="A"))

    with pytest.raises(KeyError) as excinfo:
        store.load("B")

    assert excinfo.value.args == ("B",)


def test_load_schema_invalid_payload_raises_validation_error(
    session_root: Path,
) -> None:
    from disputatio.events.state_store import FileStateStore

    bootstrap_session(session_root)
    store = FileStateStore(session_root)
    payload = make_state(session_id="sess-1").model_dump(mode="json", by_alias=True)
    del payload["limits"]
    session_json_path(session_root).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        store.load("sess-1")


def test_save_twice_second_state_fully_replaces_first(session_root: Path) -> None:
    from disputatio.events.state_store import FileStateStore

    bootstrap_session(session_root)
    store = FileStateStore(session_root)
    first = make_state(current_round=1, prompt="первый", tokens=10)
    second = make_state(
        current_round=2,
        state=SessionPhase.REVIEWING,
        prompt="второй",
        mode=Mode.ANALYZE,
        tokens=999,
    )

    store.save(first)
    store.save(second)

    assert store.load(second.session_id) == second
    on_disk = json.loads(session_json_path(session_root).read_text(encoding="utf-8"))
    assert on_disk == second.model_dump(mode="json", by_alias=True)


def test_save_serializes_by_alias_with_schema_key(session_root: Path) -> None:
    """REQ-011: сериализация — только публичным `model_dump(by_alias=True)`."""
    from disputatio.events.state_store import FileStateStore

    bootstrap_session(session_root)
    FileStateStore(session_root).save(make_state())

    on_disk = json.loads(session_json_path(session_root).read_text(encoding="utf-8"))
    assert on_disk["schema"] == "disputatio/v1"
    assert "schema_" not in on_disk


def test_file_state_store_satisfies_state_store_protocol(session_root: Path) -> None:
    from disputatio.events.state_store import FileStateStore

    store: ports.StateStore = FileStateStore(session_root)

    assert isinstance(store, ports.StateStore)
