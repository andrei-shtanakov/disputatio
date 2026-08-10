"""Механика `emit`, не покрытая `test_event_sink.py`: O_APPEND, fsync, mkdir.

Наблюдаемый результат («в файле N+1 строк, первые N не изменены») одинаков и
у append-only записи, и у `seek(0, SEEK_END)` в `"r+b"`, и без `fsync`. Эти
тесты пиньят сам механизм, на котором держатся [REQ-006]/[REQ-007]:
O_APPEND как флаг дескриптора, `flush` до `fsync`, и запрет на `mkdir`
([REQ-002]: bootstrap — единственная точка создания директорий).
"""

import fcntl
import json
import os
from pathlib import Path

import pytest

from disputatio.events.bootstrap import bootstrap_session
from disputatio.events.event_sink import JsonlEventSink
from disputatio.events.paths import events_jsonl_path, session_dir

from .test_event_sink import make_event


def test_emit_opens_descriptor_with_o_append(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-006/REQ-007: запись идёт через O_APPEND, а не через seek в конец."""
    bootstrap_session(session_root)
    flags: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        flags.append(fcntl.fcntl(fd, fcntl.F_GETFL))
        real_fsync(fd)

    monkeypatch.setattr("disputatio.events.event_sink.os.fsync", spy)

    JsonlEventSink(session_root).emit(make_event())

    assert len(flags) == 1
    assert flags[0] & os.O_APPEND


def test_emit_flushes_before_fsync(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DESIGN-004: `fsync` вызывается на уже вытолкнутых байтах, не на пустоте.

    Без `flush()` строка к моменту `fsync` живёт в буфере Python и другому
    дескриптору не видна — `fsync` синхронизировал бы нулевой хвост.
    """
    bootstrap_session(session_root)
    path = events_jsonl_path(session_root)
    seen_at_fsync: list[bytes] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        seen_at_fsync.append(path.read_bytes())
        real_fsync(fd)

    monkeypatch.setattr("disputatio.events.event_sink.os.fsync", spy)
    event = make_event()

    JsonlEventSink(session_root).emit(event)

    assert len(seen_at_fsync) == 1
    assert seen_at_fsync[0] == path.read_bytes()
    assert json.loads(seen_at_fsync[0]) == event.model_dump(mode="json", by_alias=True)


def test_emit_without_bootstrap_raises_and_creates_nothing(
    session_root: Path,
) -> None:
    """REQ-002: sink не создаёт `.disputatio/` — единственный `mkdir` в bootstrap."""
    sink = JsonlEventSink(session_root)

    with pytest.raises(FileNotFoundError):
        sink.emit(make_event())

    assert not session_dir(session_root).exists()


def test_emit_keeps_event_on_one_line_when_payload_has_newlines(
    session_root: Path,
) -> None:
    """REQ-006: одно событие — ровно одна строка, даже если в payload есть `\\n`."""
    bootstrap_session(session_root)
    event = make_event(payload={"text": "первая\nвторая\r\nтретья", "tail": "\n"})

    JsonlEventSink(session_root).emit(event)

    raw = events_jsonl_path(session_root).read_bytes()
    assert raw.count(b"\n") == 1
    assert raw.endswith(b"\n")
    assert json.loads(raw.decode("utf-8")) == event.model_dump(
        mode="json", by_alias=True
    )


def test_emit_writes_non_ascii_payload_as_utf8(session_root: Path) -> None:
    """Кодировка строки — всегда UTF-8, а не локаль-зависимый дефолт."""
    bootstrap_session(session_root)
    text = "раунд №1 — ✓"
    event = make_event(payload={"text": text})

    JsonlEventSink(session_root).emit(event)

    raw = events_jsonl_path(session_root).read_bytes()
    assert text.encode("utf-8") in raw
    assert json.loads(raw.decode("utf-8"))["payload"]["text"] == text
