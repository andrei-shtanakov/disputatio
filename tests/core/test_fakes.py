"""Тесты фейков ports и фикстур `tests/core/conftest.py`.

TASK-003, [DESIGN-009], [REQ-005], [NFR-003].

Импорты `.conftest` выполняются внутри тестов: на момент red-чекпоинта
файла ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_fakes_satisfy_runtime_checkable_ports`) превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
"""

import ast
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from disputatio.contracts import (
    Event,
    EventSink,
    EventSource,
    EventType,
    SessionPhase,
    SessionState,
    StateStore,
)

if TYPE_CHECKING:
    from .conftest import FailingStateStore, FakeEventSink, FakeStateStore

CONFTEST_PATH = Path(__file__).with_name("conftest.py")

# Тестовая обвязка ядра не трогает диск/процессы ([REQ-005], [NFR-003]):
# единственный санкционированный диск — pytest `tmp_path`, который сами
# фейки не используют.
FORBIDDEN_MODULES = frozenset(
    {"io", "os", "pathlib", "shutil", "socket", "subprocess", "tempfile"}
)
FORBIDDEN_CALLS = frozenset({"open"})


def make_event(session_id: str, ts: datetime) -> Event:
    """Минимальное событие `state_change` — груз для фейка EventSink."""
    return Event(
        ts=ts,
        session=session_id,
        round=1,
        source=EventSource.ORCHESTRATOR,
        type=EventType.STATE_CHANGE,
    )


def test_fakes_satisfy_runtime_checkable_ports() -> None:
    """Все три фейка проходят `isinstance` runtime_checkable-портов."""
    try:
        from .conftest import FailingStateStore, FakeEventSink, FakeStateStore
    except ImportError as exc:  # red-фаза: tests/core/conftest.py ещё не создан
        raise AssertionError("tests/core/conftest.py ещё не создан") from exc

    assert isinstance(FakeStateStore(), StateStore)
    assert isinstance(FakeEventSink(), EventSink)
    assert isinstance(FailingStateStore(), StateStore)


def test_shared_journal_records_save_before_emit(
    calls: list[str],
    store: "FakeStateStore",
    sink: "FakeEventSink",
    session_state: SessionState,
    fixed_now: Callable[[], datetime],
) -> None:
    """Общий журнал фиксирует порядок `save` → `emit` (write-ahead I2)."""
    assert store.calls is calls
    assert sink.calls is calls

    saved = session_state.model_copy(update={"state": SessionPhase.VERIFYING})
    store.save(saved)
    sink.emit(make_event(saved.session_id, fixed_now()))

    assert calls == ["save(phase=VERIFYING)", "emit(state_change)"]


def test_fake_event_sink_accumulates_events(
    sink: "FakeEventSink",
    session_state: SessionState,
    fixed_now: Callable[[], datetime],
) -> None:
    """`FakeEventSink` копит события в памяти в порядке `emit`."""
    first = make_event(session_state.session_id, fixed_now())
    second = first.model_copy(update={"type": EventType.ARTIFACT_WRITTEN})
    sink.emit(first)
    sink.emit(second)

    assert sink.events == [first, second]


def test_fake_store_round_trips_state_and_journals_load(
    calls: list[str],
    store: "FakeStateStore",
    session_state: SessionState,
) -> None:
    """`load` возвращает сохранённое состояние и попадает в общий журнал."""
    store.save(session_state)

    assert store.load(session_state.session_id) == session_state
    assert calls == [
        "save(phase=IDLE)",
        f"load({session_state.session_id})",
    ]


def test_fake_store_load_of_unknown_session_raises_key_error(
    store: "FakeStateStore",
) -> None:
    """Отсутствующая сессия — `KeyError`, как требует порт `StateStore`."""
    with pytest.raises(KeyError):
        store.load("нет-такой-сессии")


def test_failing_store_save_raises_and_stores_nothing(
    calls: list[str],
    failing_store: "FailingStateStore",
    session_state: SessionState,
) -> None:
    """`FailingStateStore.save` бросает; состояние не сохранено."""
    from .conftest import StoreFailure

    with pytest.raises(StoreFailure):
        failing_store.save(session_state)

    assert calls == ["save_failed(phase=IDLE)"]
    with pytest.raises(KeyError):
        failing_store.load(session_state.session_id)


def test_fakes_default_to_their_own_journal() -> None:
    """Без аргумента каждый фейк заводит собственный пустой журнал."""
    from .conftest import FakeEventSink, FakeStateStore

    store = FakeStateStore()
    sink = FakeEventSink()

    assert store.calls == []
    assert sink.calls == []
    assert store.calls is not sink.calls


def test_fixed_now_is_deterministic(fixed_now: Callable[[], datetime]) -> None:
    """Инжектированные часы: два вызова дают одно и то же aware-значение."""
    from .conftest import FIXED_NOW

    first = fixed_now()
    second = fixed_now()

    assert first == second
    assert first == FIXED_NOW
    assert first.utcoffset() == UTC.utcoffset(None)


def test_fakes_do_not_write_to_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: "FakeStateStore",
    sink: "FakeEventSink",
    failing_store: "FailingStateStore",
    session_state: SessionState,
    fixed_now: Callable[[], datetime],
) -> None:
    """Полный прогон фейков не создаёт ни одного файла в рабочем каталоге."""
    from .conftest import StoreFailure

    monkeypatch.chdir(tmp_path)
    store.save(session_state)
    store.load(session_state.session_id)
    sink.emit(make_event(session_state.session_id, fixed_now()))
    with pytest.raises(StoreFailure):
        failing_store.save(session_state)

    assert list(tmp_path.iterdir()) == []


def test_conftest_source_imports_no_io_modules() -> None:
    """Статический запрет I/O в исходнике `conftest.py` ([NFR-003])."""
    tree = ast.parse(CONFTEST_PATH.read_text(encoding="utf-8"))

    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module is not None:
                imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)

    assert imported & FORBIDDEN_MODULES == set()
    assert called & FORBIDDEN_CALLS == set()
