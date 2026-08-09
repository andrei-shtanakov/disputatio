"""Тесты `rounds.py`: TASK-006, [DESIGN-005], [REQ-008], [REQ-009], [REQ-012].

Импорт `disputatio.events.rounds` внутри тестов: на момент red-чекпоинта
модуля ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_write_creates_round_dir_and_file`) превращает `ImportError` в
`AssertionError` — гейт принимает red только при падении assertion'ом.
"""

from pathlib import Path

import pytest

from disputatio.events import atomic
from disputatio.events.bootstrap import bootstrap_session
from disputatio.events.paths import round_dir, rounds_dir

FINALIZED_MARKER = ".finalized"

PROPOSAL_MD = (
    "---\n"
    "round: 3\n"
    'summary: "правка парсера: \\"кавычки\\" и \\\\ слэш"\n'
    "files: [src/a.py, src/b.py]\n"
    "---\n"
    "\n"
    "# Предложение\n"
    "\n"
    "Юникод — ёжик 🦔, таб:\tи CR внутри строки:\r\n"
    "\n"
    "```json\n"
    '{"escaped": "не должно быть перекодировано"}\n'
    "```\n"
)


def _raise_os_error(*args: object, **kwargs: object) -> None:
    """Подмена `os.replace`, имитирующая сбой на финальном шаге записи."""
    raise OSError("boom")


def test_write_creates_round_dir_and_file(session_root: Path) -> None:
    try:
        from disputatio.events.rounds import write_round_artifact
    except ImportError as exc:  # red-фаза: rounds.py ещё не создан
        raise AssertionError("src/disputatio/events/rounds.py ещё не создан") from exc

    bootstrap_session(session_root)
    assert not round_dir(session_root, 3).exists()

    path = write_round_artifact(session_root, 3, "proposal.md", "# Предложение\n")

    assert path == rounds_dir(session_root) / "003" / "proposal.md"
    assert path.read_text(encoding="utf-8") == "# Предложение\n"
    assert round_dir(session_root, 3).is_dir()


def test_missing_optional_artifact_is_not_an_error(session_root: Path) -> None:
    """SPEC-001 §3: `changes.patch` может отсутствовать (analyze-раунд)."""
    from disputatio.events.rounds import finalize_round, write_round_artifact

    bootstrap_session(session_root)

    write_round_artifact(session_root, 4, "proposal.md", "анализ без правок\n")
    write_round_artifact(session_root, 4, "review.json", '{"verdict": "approve"}')
    finalize_round(session_root, 4)

    assert not (round_dir(session_root, 4) / "changes.patch").exists()
    assert sorted(p.name for p in round_dir(session_root, 4).iterdir()) == [
        FINALIZED_MARKER,
        "proposal.md",
        "review.json",
    ]


def test_rewrite_current_unfinalized_round_succeeds(session_root: Path) -> None:
    """PROPOSING re-runnable: до `finalize_round` перезапись разрешена."""
    from disputatio.events.rounds import write_round_artifact

    bootstrap_session(session_root)

    write_round_artifact(session_root, 3, "proposal.md", "первая попытка\n")
    write_round_artifact(session_root, 3, "proposal.md", "вторая попытка\n")
    path = write_round_artifact(session_root, 3, "proposal.md", "третья попытка\n")

    assert path.read_text(encoding="utf-8") == "третья попытка\n"


def test_finalize_then_write_raises_round_immutable_error(session_root: Path) -> None:
    """I3: артефакт финализированного раунда не меняется задним числом."""
    from disputatio.events.rounds import (
        RoundImmutableError,
        finalize_round,
        write_round_artifact,
    )

    bootstrap_session(session_root)
    original = '{"verdict": "request_changes"}'
    write_round_artifact(session_root, 2, "review.json", original)
    finalize_round(session_root, 2)

    with pytest.raises(RoundImmutableError):
        write_round_artifact(session_root, 2, "review.json", '{"verdict": "approve"}')

    assert (round_dir(session_root, 2) / "review.json").read_text(
        encoding="utf-8"
    ) == original


def test_finalized_round_rejects_new_artifact_without_touching_dir(
    session_root: Path,
) -> None:
    """Запись не начинается: нового файла нет, временных файлов не осталось."""
    from disputatio.events.rounds import (
        RoundImmutableError,
        finalize_round,
        write_round_artifact,
    )

    bootstrap_session(session_root)
    write_round_artifact(session_root, 2, "proposal.md", "предложение\n")
    finalize_round(session_root, 2)

    with pytest.raises(RoundImmutableError):
        write_round_artifact(session_root, 2, "decision.json", '{"action": "accept"}')

    assert not (round_dir(session_root, 2) / "decision.json").exists()
    assert sorted(p.name for p in round_dir(session_root, 2).iterdir()) == [
        FINALIZED_MARKER,
        "proposal.md",
    ]


def test_finalize_round_idempotent(session_root: Path) -> None:
    """ADR-004: маркер уже есть → no-op, не ошибка."""
    from disputatio.events.rounds import finalize_round

    bootstrap_session(session_root)

    finalize_round(session_root, 5)
    finalize_round(session_root, 5)

    marker = round_dir(session_root, 5) / FINALIZED_MARKER
    assert marker.is_file()


def test_finalize_round_does_not_freeze_other_rounds(session_root: Path) -> None:
    """Маркер — на директорию раунда, а не на сессию целиком (ADR-004)."""
    from disputatio.events.rounds import finalize_round, write_round_artifact

    bootstrap_session(session_root)
    write_round_artifact(session_root, 1, "proposal.md", "раунд 1\n")
    finalize_round(session_root, 1)

    path = write_round_artifact(session_root, 2, "proposal.md", "раунд 2\n")

    assert path.read_text(encoding="utf-8") == "раунд 2\n"
    assert not (round_dir(session_root, 2) / FINALIZED_MARKER).exists()


def test_write_round_artifact_proposal_md_written_verbatim(
    session_root: Path,
) -> None:
    """REQ-012: markdown с YAML-фронтматтером — побайтово, без пересериализации."""
    from disputatio.events.rounds import write_round_artifact

    bootstrap_session(session_root)

    path = write_round_artifact(session_root, 7, "proposal.md", PROPOSAL_MD)

    assert path.read_bytes() == PROPOSAL_MD.encode("utf-8")


def test_write_round_artifact_replaces_by_rename_without_leftovers(
    session_root: Path,
) -> None:
    """REQ-001: перезапись — temp-file + rename (новый inode), без `.tmp`-мусора."""
    from disputatio.events.rounds import write_round_artifact

    bootstrap_session(session_root)
    first = write_round_artifact(session_root, 3, "verification.json", '{"n": 1}')
    inode_before = first.stat().st_ino

    write_round_artifact(session_root, 3, "verification.json", '{"n": 2}')

    assert first.stat().st_ino != inode_before
    assert list(round_dir(session_root, 3).glob("*.tmp*")) == []


def test_write_round_artifact_failure_at_rename_leaves_previous_content(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-001: сбой на `os.replace` не разрушает уже записанный артефакт."""
    from disputatio.events.rounds import write_round_artifact

    bootstrap_session(session_root)
    path = write_round_artifact(session_root, 3, "decision.json", '{"action": "keep"}')

    monkeypatch.setattr(atomic.os, "replace", _raise_os_error)

    with pytest.raises(OSError):
        write_round_artifact(session_root, 3, "decision.json", '{"action": "lost"}')

    assert path.read_text(encoding="utf-8") == '{"action": "keep"}'


def test_finalize_round_marker_written_atomically(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой на `os.replace` не оставляет полу-финализированный раунд."""
    from disputatio.events.rounds import finalize_round, write_round_artifact

    bootstrap_session(session_root)
    write_round_artifact(session_root, 6, "proposal.md", "раунд 6\n")

    monkeypatch.setattr(atomic.os, "replace", _raise_os_error)

    with pytest.raises(OSError):
        finalize_round(session_root, 6)

    assert not (round_dir(session_root, 6) / FINALIZED_MARKER).exists()
