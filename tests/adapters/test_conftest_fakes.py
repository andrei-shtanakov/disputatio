"""Тесты TASK-001: каркас `disputatio.adapters` и контракт фейков conftest.

[DESIGN-010], NFR-002, NFR-003. Red-цикл:
`test_adapters_package_importable` падает assertion'ом, пока
`src/disputatio/adapters/__init__.py` не создан (green-шаг задачи);
остальные тесты — regression-пины наблюдаемого поведения тестовой
инфраструктуры, зелёные с рождения (конституция: regression-тест
фиксирует наблюдаемое поведение, red-селектор — только первый тест).

Импорт фейков — штатный `adapters.conftest`: у `tests/` нет
`__init__.py`, pytest кладёт каталог `tests/` на `sys.path`, а пакет
`adapters` имеет `__init__.py`, поэтому модуль одинаково резолвится
pytest'ом и pyrefly (search-path включает `tests`). Повторной
регистрации фикстур не происходит: после автозагрузки conftest модуль
уже лежит в `sys.modules` под тем же именем.
"""

import importlib.util
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import anyio

from adapters.conftest import (
    FIXED_TS,
    FakeProcess,
    FakeSpawn,
    RecordingSink,
    SpawnCall,
)
from disputatio.contracts.events import Event, EventSource, EventType
from disputatio.contracts.ports import EventSink


def test_adapters_package_importable() -> None:
    """Каркас пакета: `disputatio.adapters` существует и импортируется."""
    spec = importlib.util.find_spec("disputatio.adapters")
    assert spec is not None, "src/disputatio/adapters/__init__.py ещё не создан"


def test_fake_process_yields_prepared_lines() -> None:
    """`FakeProcess.stdout` отдаёт заготовленные строки байтами с `\\n`."""
    process = FakeProcess(["alpha", "beta"], returncode=3, stderr=b"boom")

    async def collect() -> list[bytes]:
        return [line async for line in process.stdout]

    assert anyio.run(collect) == [b"alpha\n", b"beta\n"]
    assert process.returncode == 3
    assert process.stderr == b"boom"


def test_fake_spawn_records_call(tmp_path: Path) -> None:
    """`FakeSpawn` записывает `(argv, cwd, stdin)` и отдаёт свой процесс."""
    spawn = FakeSpawn()
    stdin_marker = object()

    async def call() -> FakeProcess:
        return await spawn("claude", "-p", cwd=tmp_path, stdin=stdin_marker)

    assert anyio.run(call) is spawn.process
    assert spawn.calls == [
        SpawnCall(argv=("claude", "-p"), cwd=tmp_path, stdin=stdin_marker)
    ]


def test_recording_sink_accepts_events(
    recording_sink: RecordingSink, fixed_clock: Callable[[], datetime]
) -> None:
    """`RecordingSink` структурно удовлетворяет `EventSink` и копит `Event`."""
    assert isinstance(recording_sink, EventSink)
    event = Event(
        ts=fixed_clock(),
        session="sess-20260101-01",
        source=EventSource.AUTHOR,
        type=EventType.AGENT_TEXT_DELTA,
        payload={"text": "дельта"},
    )
    recording_sink.emit(event)
    assert recording_sink.events == [event]
    assert recording_sink.events[0].ts == FIXED_TS


def test_native_stream_fixtures_pin_format(
    claude_stream_lines: list[str], codex_stream_lines: list[str]
) -> None:
    """Фикстуры потоков пинуют кромки форматов и несут raw-строку."""
    first_claude = json.loads(claude_stream_lines[0])
    last_claude = json.loads(claude_stream_lines[-1])
    assert first_claude["type"] == "system"
    assert last_claude["type"] == "result"
    assert "usage" in last_claude
    assert "непарсибельная строка claude" in claude_stream_lines

    first_codex = json.loads(codex_stream_lines[0])
    last_codex = json.loads(codex_stream_lines[-1])
    assert first_codex["type"] == "thread.started"
    assert last_codex["type"] == "turn.completed"
    assert "usage" in last_codex
    assert "непарсибельная строка codex" in codex_stream_lines
