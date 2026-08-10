"""Имя артефакта раунда как граница I3: `rounds.py`, [REQ-008], [REQ-009].

Отдельный файл рядом с байт-локнутым `test_round_artifacts.py` (claim
TASK-006): тот пинит happy-path и маркер финализации, здесь — дыры, через
которые запись уходила мимо раунда, для которого проверялся `.finalized`.
"""

from pathlib import Path

import pytest

from disputatio.events.bootstrap import bootstrap_session
from disputatio.events.paths import round_dir, rounds_dir, session_dir
from disputatio.events.rounds import (
    FINALIZED_MARKER_NAME,
    RoundImmutableError,
    finalize_round,
    write_round_artifact,
)


def test_relative_path_in_name_cannot_touch_finalized_round(
    session_root: Path,
) -> None:
    """I3: `../002/...` из текущего раунда не переписывает финализированный."""
    bootstrap_session(session_root)
    write_round_artifact(session_root, 2, "review.json", '{"verdict": "reject"}')
    finalize_round(session_root, 2)

    with pytest.raises(ValueError):
        write_round_artifact(
            session_root, 3, "../002/review.json", '{"verdict": "approve"}'
        )

    assert (round_dir(session_root, 2) / "review.json").read_text(
        encoding="utf-8"
    ) == '{"verdict": "reject"}'
    assert not round_dir(session_root, 3).exists()


def test_absolute_path_in_name_rejected(session_root: Path, tmp_path: Path) -> None:
    """`Path / "/abs"` даёт абсолютный путь — запись ушла бы вне сессии."""
    bootstrap_session(session_root)
    outside = tmp_path / "outside.json"

    with pytest.raises(ValueError):
        write_round_artifact(session_root, 1, str(outside), "секрет")

    assert not outside.exists()


def test_backslash_in_name_rejected(session_root: Path) -> None:
    bootstrap_session(session_root)

    with pytest.raises(ValueError):
        write_round_artifact(session_root, 1, "sub\\review.json", "{}")

    assert not round_dir(session_root, 1).exists()


def test_finalized_marker_name_rejected_as_artifact(session_root: Path) -> None:
    """Раунд нельзя «финализировать» записью артефакта с именем маркера."""
    bootstrap_session(session_root)

    with pytest.raises(ValueError):
        write_round_artifact(session_root, 4, FINALIZED_MARKER_NAME, "подделка")

    assert not (round_dir(session_root, 4) / FINALIZED_MARKER_NAME).exists()
    path = write_round_artifact(session_root, 4, "proposal.md", "раунд 4\n")
    assert path.read_text(encoding="utf-8") == "раунд 4\n"


@pytest.mark.parametrize("name", ["", ".", ".."])
def test_degenerate_name_rejected_without_filesystem_litter(
    session_root: Path, name: str
) -> None:
    """Пустое/точечное имя раньше падало `IsADirectoryError`, оставив `.tmp`."""
    bootstrap_session(session_root)

    with pytest.raises(ValueError):
        write_round_artifact(session_root, 9, name, "{}")

    assert list(rounds_dir(session_root).iterdir()) == []


def test_write_creates_whole_round_tree_without_bootstrap(session_root: Path) -> None:
    """`mkdir(parents=True)`: bootstrap не предусловие для записи артефакта."""
    assert not session_dir(session_root).exists()

    path = write_round_artifact(session_root, 3, "proposal.md", "без bootstrap\n")

    assert path.read_text(encoding="utf-8") == "без bootstrap\n"


def test_finalize_round_marker_is_empty(session_root: Path) -> None:
    """Маркер — факт, а не данные: содержимое пустое ([ADR-004])."""
    bootstrap_session(session_root)

    finalize_round(session_root, 5)

    assert (round_dir(session_root, 5) / FINALIZED_MARKER_NAME).read_bytes() == b""


def test_finalize_round_creates_tree_without_bootstrap(session_root: Path) -> None:
    finalize_round(session_root, 1)

    assert (round_dir(session_root, 1) / FINALIZED_MARKER_NAME).is_file()
    with pytest.raises(RoundImmutableError):
        write_round_artifact(session_root, 1, "proposal.md", "поздно\n")
