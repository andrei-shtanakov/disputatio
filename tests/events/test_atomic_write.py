"""Тесты `atomic.py::atomic_write`: TASK-002, [DESIGN-001], [REQ-001], [ADR-001].

Импорт `disputatio.events.atomic` внутри тестов: на момент red-чекпоинта
модуля ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_atomic_write_creates_file_with_str_content`) превращает `ImportError`
в `AssertionError` — гейт принимает red только при падении assertion'ом.
"""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest


def test_atomic_write_creates_file_with_str_content(tmp_path: Path) -> None:
    try:
        from disputatio.events.atomic import atomic_write
    except ImportError as exc:  # red-фаза: atomic.py ещё не создан
        raise AssertionError("src/disputatio/events/atomic.py ещё не создан") from exc

    path = tmp_path / "target.txt"
    atomic_write(path, "hello world")

    assert path.read_text(encoding="utf-8") == "hello world"


def test_atomic_write_creates_file_with_bytes_content(tmp_path: Path) -> None:
    from disputatio.events.atomic import atomic_write

    path = tmp_path / "target.bin"
    atomic_write(path, b"\x00\x01\x02binary")

    assert path.read_bytes() == b"\x00\x01\x02binary"


def test_atomic_write_overwrites_existing_file_completely(tmp_path: Path) -> None:
    from disputatio.events.atomic import atomic_write

    path = tmp_path / "target.txt"
    atomic_write(path, "a much longer previous content that should be gone")
    atomic_write(path, "short")

    assert path.read_text(encoding="utf-8") == "short"


def test_atomic_write_failure_leaves_previous_content_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from disputatio.events import atomic

    path = tmp_path / "target.txt"
    atomic_write = atomic.atomic_write
    atomic_write(path, "previous content")

    monkeypatch.setattr(atomic.os, "replace", Mock(side_effect=OSError("boom")))

    with pytest.raises(OSError):
        atomic_write(path, "new content that must never land")

    assert path.read_text(encoding="utf-8") == "previous content"


def test_atomic_write_failure_when_target_did_not_exist_leaves_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from disputatio.events import atomic

    path = tmp_path / "target.txt"
    monkeypatch.setattr(atomic.os, "replace", Mock(side_effect=OSError("boom")))

    with pytest.raises(OSError):
        atomic.atomic_write(path, "new content")

    assert not path.exists()


def test_temp_file_created_in_same_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from disputatio.events import atomic

    path = tmp_path / "target.txt"
    monkeypatch.setattr(atomic.os, "replace", Mock(side_effect=OSError("boom")))

    with pytest.raises(OSError):
        atomic.atomic_write(path, "content")

    leftovers = list(tmp_path.glob("*.tmp*"))
    assert len(leftovers) == 1
    assert leftovers[0].parent == tmp_path


def test_close_called_even_if_replace_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from disputatio.events import atomic

    path = tmp_path / "target.txt"
    close_calls: list[int] = []
    original_close = os.close

    def spy_close(fd: int) -> None:
        close_calls.append(fd)
        original_close(fd)

    monkeypatch.setattr(atomic.os, "close", spy_close)
    monkeypatch.setattr(atomic.os, "replace", Mock(side_effect=OSError("boom")))

    with pytest.raises(OSError):
        atomic.atomic_write(path, "content")

    assert len(close_calls) == 1
