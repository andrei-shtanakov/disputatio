"""Тесты `paths.py`: TASK-001, [DESIGN §3], [REQ-002], [REQ-008].

Импорт `disputatio.events.paths` внутри теста: на момент red-чекпоинта
модуля ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_session_dir_under_root`) превращает `ImportError` в `AssertionError` —
гейт принимает red только при падении assertion'ом.
"""

from pathlib import Path


def test_session_dir_under_root(session_root: Path) -> None:
    try:
        from disputatio.events.paths import session_dir
    except ImportError as exc:  # red-фаза: paths.py ещё не создан
        raise AssertionError("src/disputatio/events/paths.py ещё не создан") from exc

    assert session_dir(session_root) == session_root / ".disputatio"


def test_round_dir_zero_padded(session_root: Path) -> None:
    from disputatio.events.paths import round_dir

    assert round_dir(session_root, 3) == (
        session_root / ".disputatio" / "rounds" / "003"
    )


def test_round_dir_zero_padded_two_digit_input(session_root: Path) -> None:
    from disputatio.events.paths import round_dir

    assert round_dir(session_root, 12) == (
        session_root / ".disputatio" / "rounds" / "012"
    )


def test_result_dir_under_session_dir(session_root: Path) -> None:
    from disputatio.events.paths import result_dir

    assert result_dir(session_root) == session_root / ".disputatio" / "result"


def test_session_json_path_under_session_dir(session_root: Path) -> None:
    from disputatio.events.paths import session_json_path

    assert session_json_path(session_root) == (
        session_root / ".disputatio" / "session.json"
    )


def test_config_toml_path_under_session_dir(session_root: Path) -> None:
    from disputatio.events.paths import config_toml_path

    assert config_toml_path(session_root) == (
        session_root / ".disputatio" / "config.toml"
    )


def test_events_jsonl_path_under_session_dir(session_root: Path) -> None:
    from disputatio.events.paths import events_jsonl_path

    assert events_jsonl_path(session_root) == (
        session_root / ".disputatio" / "events.jsonl"
    )
