"""Тесты `export.py`: TASK-007, [DESIGN-006], [REQ-010], [REQ-001], [ADR-005].

Импорт `disputatio.events.export` внутри тестов: на момент red-чекпоинта
модуля ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_write_result_writes_each_file_atomically`) превращает `ImportError` в
`AssertionError` — гейт принимает red только при падении assertion'ом.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from disputatio.events import atomic
from disputatio.events.bootstrap import bootstrap_session
from disputatio.events.paths import result_dir

RESULT_MD = "# Результат\n\nЮникод — ёжик 🦔, таб:\tи CR:\r\nконец\n"
RESULT_JSON = '{"verdict": "approve", "текст": "не перекодировать"}'


def _raise_os_error(*args: object, **kwargs: object) -> None:
    """Подмена `os.replace`, имитирующая сбой на финальном шаге записи."""
    raise OSError("boom")


def _read_manifest(root: Path) -> dict[str, Any]:
    """Разбирает `result/manifest.json` как JSON-объект."""
    raw = (result_dir(root) / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    assert isinstance(manifest, dict)
    return manifest


def test_write_result_writes_each_file_atomically(session_root: Path) -> None:
    try:
        from disputatio.events.export import write_result
    except ImportError as exc:  # red-фаза: export.py ещё не создан
        raise AssertionError("src/disputatio/events/export.py ещё не создан") from exc

    bootstrap_session(session_root)

    write_result(
        session_root,
        files={"result.md": RESULT_MD, "result.json": RESULT_JSON},
        manifest={"round": 5},
    )

    assert (result_dir(session_root) / "result.md").read_bytes() == RESULT_MD.encode(
        "utf-8"
    )
    assert (
        result_dir(session_root) / "result.json"
    ).read_bytes() == RESULT_JSON.encode("utf-8")
    assert list(result_dir(session_root).glob("*.tmp*")) == []
    assert list(result_dir(session_root).glob(".*.tmp*")) == []


def test_manifest_json_includes_sha256_per_file(session_root: Path) -> None:
    """REQ-010: checksum считается по тем же байтам, что легли на диск."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)

    write_result(
        session_root,
        files={"result.md": RESULT_MD, "result.json": RESULT_JSON},
        manifest={"round": 5},
    )

    files = _read_manifest(session_root)["files"]
    assert set(files) == {"result.md", "result.json"}
    assert (
        files["result.md"]["sha256"]
        == hashlib.sha256(RESULT_MD.encode("utf-8")).hexdigest()
    )
    assert (
        files["result.json"]["sha256"]
        == hashlib.sha256(RESULT_JSON.encode("utf-8")).hexdigest()
    )


def test_manifest_preserves_caller_fields_verbatim(session_root: Path) -> None:
    """ADR-005: store не интерпретирует манифест — только сериализует."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)
    caller: dict[str, Any] = {
        "converged": True,
        "round": 5,
        "mode": "implement",
        "nested": {"gates": [{"name": "pytest", "status": "pass"}], "цифра": 1},
        "empty_list": [],
        "none_value": None,
    }

    write_result(session_root, files={"result.md": RESULT_MD}, manifest=caller)

    manifest = _read_manifest(session_root)
    for key, value in caller.items():
        assert manifest[key] == value
    assert set(manifest) == set(caller) | {"files"}


def test_partial_export_converged_false_with_open_issues(session_root: Path) -> None:
    """REQ-010: partial-экспорт (ESCALATED) честно пишет `converged: false`."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)
    open_issues = [
        "review round 5: missing test for edge case X",
        "verification: ruff check . failed",
    ]

    write_result(
        session_root,
        files={"result.md": RESULT_MD},
        manifest={"converged": False, "round": 5, "open_issues": open_issues},
    )

    manifest = _read_manifest(session_root)
    assert manifest["converged"] is False
    assert manifest["open_issues"] == open_issues
    assert '"converged": false' in (
        result_dir(session_root) / "manifest.json"
    ).read_text(encoding="utf-8")


def test_computed_files_key_wins_over_caller_files_field(session_root: Path) -> None:
    """`files` — вычисляемый ключ: подделать checksum через манифест нельзя."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)

    write_result(
        session_root,
        files={"result.md": RESULT_MD},
        manifest={"files": {"result.md": {"sha256": "deadbeef"}}, "converged": True},
    )

    files = _read_manifest(session_root)["files"]
    assert files == {
        "result.md": {"sha256": hashlib.sha256(RESULT_MD.encode("utf-8")).hexdigest()}
    }


def test_manifest_json_cannot_be_supplied_as_exported_file(session_root: Path) -> None:
    """Иначе манифест затирал бы файл, checksum которого сам же обещает."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)

    with pytest.raises(ValueError):
        write_result(
            session_root,
            files={"manifest.json": '{"подделка": true}'},
            manifest={"converged": True},
        )

    assert not (result_dir(session_root) / "manifest.json").exists()


@pytest.mark.parametrize(
    "name", ["../escaped.md", "sub/result.md", "..\\escaped.md", "", ".", ".."]
)
def test_file_name_must_be_simple_name_inside_result(
    session_root: Path, name: str
) -> None:
    """REQ-010: экспорт пишет строго `result/*`, а не мимо директории."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)

    with pytest.raises(ValueError):
        write_result(session_root, files={name: "утечка\n"}, manifest={})

    assert list(result_dir(session_root).iterdir()) == []
    assert not (result_dir(session_root).parent / "escaped.md").exists()


def test_bad_file_name_rejected_before_any_file_is_written(session_root: Path) -> None:
    """Валидация имён — до первой записи: частичного экспорта не остаётся."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)

    with pytest.raises(ValueError):
        write_result(
            session_root,
            files={"result.md": RESULT_MD, "../escaped.md": "утечка\n"},
            manifest={},
        )

    assert list(result_dir(session_root).iterdir()) == []


def test_write_result_replaces_by_rename_without_leftovers(session_root: Path) -> None:
    """REQ-001: повторный экспорт — temp-file + rename (новый inode)."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)
    write_result(session_root, files={"result.md": "первый\n"}, manifest={"round": 1})
    path = result_dir(session_root) / "result.md"
    inode_before = path.stat().st_ino
    manifest_inode_before = (result_dir(session_root) / "manifest.json").stat().st_ino

    write_result(session_root, files={"result.md": "второй\n"}, manifest={"round": 2})

    assert path.read_text(encoding="utf-8") == "второй\n"
    assert path.stat().st_ino != inode_before
    assert (
        result_dir(session_root) / "manifest.json"
    ).stat().st_ino != manifest_inode_before
    assert list(result_dir(session_root).glob("*.tmp*")) == []
    assert list(result_dir(session_root).glob(".*.tmp*")) == []


def test_write_result_failure_at_rename_leaves_previous_export(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-001: сбой на `os.replace` не разрушает уже экспортированный файл.

    Прежний `manifest.json` при этом не сохраняется: он снимается до первой
    перезаписи, потому что пережить её и остаться правдой он не может (см.
    `test_interrupted_re_export_leaves_no_stale_manifest`). Утверждение
    теста — про пофайловую атомарность содержимого, а не про marker.

    Имя теста историческое: «previous export» здесь — содержимое отдельного
    файла, а не набор целиком. Правило набора (SPEC-002 §8.2): уже
    экспортированный отдельный файл остаётся целым; при прерванном повторном
    экспорте commit marker удаляется; набор считается невалидным до
    успешного нового marker.
    """
    from disputatio.events.export import write_result

    bootstrap_session(session_root)
    write_result(session_root, files={"result.md": "первый\n"}, manifest={"round": 1})

    monkeypatch.setattr(atomic.os, "replace", _raise_os_error)

    with pytest.raises(OSError):
        write_result(
            session_root, files={"result.md": "второй\n"}, manifest={"round": 2}
        )

    assert (result_dir(session_root) / "result.md").read_text(
        encoding="utf-8"
    ) == "первый\n"
    assert not (result_dir(session_root) / "manifest.json").exists()


def test_interrupted_re_export_leaves_no_stale_manifest(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обрыв ПОСРЕДИ перезаписи набора не оставляет прежний манифест.

    Симметрия к C1 (`runtime/pipeline_export.py`), тот же инвариант:
    манифест — commit marker, и он обязан описывать содержимое, лежащее
    рядом. Первый файл перезаписан, второй нет — прежний манифест, дожив
    до этого момента, объявляет валидным набор, чьи sha256 половине файлов
    уже не соответствуют ([REQ-010]). Экспорт сессии переигрывается на
    resume (переход в `DONE` пишется ПОСЛЕ записи), так что перезапись
    поверх готового `result/` — штатный путь, а не экзотика.
    """
    from disputatio.events.export import write_result

    bootstrap_session(session_root)
    write_result(
        session_root,
        files={"result.md": "первый\n", "result.patch": "diff-1\n"},
        manifest={"round": 1},
    )

    real_replace = atomic.os.replace

    def _fail_on_second_file(source: Any, target: Any) -> None:
        if Path(target).name == "result.patch":
            raise OSError("симулированный обрыв посреди перезаписи набора")
        real_replace(source, target)

    monkeypatch.setattr(atomic.os, "replace", _fail_on_second_file)
    with pytest.raises(OSError):
        write_result(
            session_root,
            files={"result.md": "второй\n", "result.patch": "diff-2\n"},
            manifest={"round": 2},
        )

    assert (result_dir(session_root) / "result.md").read_text(
        encoding="utf-8"
    ) == "второй\n"
    assert not (result_dir(session_root) / "manifest.json").exists(), (
        "манифест раунда 1 пережил обрыв перезаписи: commit marker обещает "
        "sha256 файлов, половина которых уже другая"
    )


def test_manifest_written_after_files_so_it_never_lists_missing_file(
    session_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Манифест не должен обещать checksum файла, который не записался."""
    from disputatio.events.export import write_result

    bootstrap_session(session_root)

    monkeypatch.setattr(atomic.os, "replace", _raise_os_error)

    with pytest.raises(OSError):
        write_result(session_root, files={"result.md": RESULT_MD}, manifest={})

    assert not (result_dir(session_root) / "manifest.json").exists()


def test_write_result_requires_bootstrapped_session(session_root: Path) -> None:
    """`bootstrap_session` — единственная точка `mkdir` статических директорий."""
    from disputatio.events.export import write_result

    with pytest.raises(FileNotFoundError):
        write_result(session_root, files={"result.md": RESULT_MD}, manifest={})
