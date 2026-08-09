"""Тесты `bootstrap.py`: TASK-003, [DESIGN-002], [REQ-002], [REQ-003], [ADR-002].

Импорт `disputatio.events.bootstrap` внутри тестов: на момент red-чекпоинта
модуля ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_bootstrap_session_creates_session_directories`) превращает `ImportError`
в `AssertionError` — гейт принимает red только при падении assertion'ом.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from disputatio.events import atomic
from disputatio.events.paths import (
    config_toml_path,
    result_dir,
    session_dir,
    session_json_path,
)

TOML_SNAPSHOT = """[agents.author]
adapter = "claude_code"
model = "claude-sonnet-5"

[limits]
max_rounds = 8
"""


def test_bootstrap_session_creates_session_directories(session_root: Path) -> None:
    try:
        from disputatio.events.bootstrap import bootstrap_session
    except ImportError as exc:  # red-фаза: bootstrap.py ещё не создан
        raise AssertionError(
            "src/disputatio/events/bootstrap.py ещё не создан"
        ) from exc

    bootstrap_session(session_root)

    assert session_dir(session_root).is_dir()
    assert (session_dir(session_root) / "rounds").is_dir()
    assert result_dir(session_root).is_dir()


def test_bootstrap_session_returns_session_dir(session_root: Path) -> None:
    from disputatio.events.bootstrap import bootstrap_session

    assert bootstrap_session(session_root) == session_dir(session_root)


def test_bootstrap_session_is_idempotent(session_root: Path) -> None:
    from disputatio.events.bootstrap import bootstrap_session

    bootstrap_session(session_root)
    bootstrap_session(session_root)

    assert session_dir(session_root).is_dir()
    assert (session_dir(session_root) / "rounds").is_dir()
    assert result_dir(session_root).is_dir()


def test_bootstrap_session_does_not_touch_existing_session_json(
    session_root: Path,
) -> None:
    from disputatio.events.bootstrap import bootstrap_session

    bootstrap_session(session_root)
    marker = '{"session_id": "existing", "state": "PROPOSING"}'
    session_json_path(session_root).write_text(marker, encoding="utf-8")

    bootstrap_session(session_root)

    assert session_json_path(session_root).read_text(encoding="utf-8") == marker


def test_write_config_snapshot_writes_toml_text_verbatim(session_root: Path) -> None:
    from disputatio.events.bootstrap import bootstrap_session, write_config_snapshot

    bootstrap_session(session_root)
    write_config_snapshot(session_root, TOML_SNAPSHOT)

    assert config_toml_path(session_root).read_bytes() == TOML_SNAPSHOT.encode("utf-8")


def test_write_config_snapshot_overwrites_previous_snapshot_completely(
    session_root: Path,
) -> None:
    from disputatio.events.bootstrap import bootstrap_session, write_config_snapshot

    bootstrap_session(session_root)
    write_config_snapshot(session_root, TOML_SNAPSHOT)
    write_config_snapshot(session_root, "[limits]\n")

    assert config_toml_path(session_root).read_bytes() == b"[limits]\n"


def test_write_config_snapshot_failure_leaves_previous_snapshot_intact(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Запись идёт через `atomic_write` ([REQ-003] → [REQ-001])."""
    from disputatio.events.bootstrap import bootstrap_session, write_config_snapshot

    bootstrap_session(session_root)
    write_config_snapshot(session_root, TOML_SNAPSHOT)

    monkeypatch.setattr(atomic.os, "replace", Mock(side_effect=OSError("boom")))

    with pytest.raises(OSError):
        write_config_snapshot(session_root, "[limits]\nmax_rounds = 99\n")

    assert config_toml_path(session_root).read_bytes() == TOML_SNAPSHOT.encode("utf-8")
