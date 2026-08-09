"""Regression-тесты `export.py` по итогам ревью TASK-007 ([REQ-010], [REQ-001]).

Основной набор — `test_export.py` (байт-locked red-чекпоинтом). Здесь закрыты
две дыры, которые он не пинил: регистр имени манифеста на case-insensitive ФС
и несериализуемый манифест, ломающий экспорт уже после записи файлов.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from disputatio.events.bootstrap import bootstrap_session
from disputatio.events.export import write_result
from disputatio.events.paths import result_dir

CONTENT = "# Результат\n"


def _read_manifest(root: Path) -> dict[str, Any]:
    """Разбирает `result/manifest.json` как JSON-объект."""
    raw = (result_dir(root) / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    assert isinstance(manifest, dict)
    return manifest


@pytest.mark.parametrize("name", ["Manifest.json", "MANIFEST.JSON", "manifest.JSON"])
def test_manifest_name_is_reserved_case_insensitively(
    session_root: Path, name: str
) -> None:
    """На APFS/NTFS `Manifest.json` — тот же файл, что и манифест экспорта."""
    bootstrap_session(session_root)

    with pytest.raises(ValueError):
        write_result(session_root, files={name: CONTENT}, manifest={"converged": True})

    assert list(result_dir(session_root).iterdir()) == []


def test_unserializable_manifest_rejected_before_any_file_is_written(
    session_root: Path,
) -> None:
    """Иначе прежний манифест остаётся рядом с уже перезаписанными файлами."""
    bootstrap_session(session_root)
    write_result(session_root, files={"result.md": "первый\n"}, manifest={"round": 1})

    with pytest.raises(TypeError):
        write_result(
            session_root,
            files={"result.md": "второй\n"},
            manifest={"round": 2, "path": Path("/tmp/x")},
        )

    assert (result_dir(session_root) / "result.md").read_text(
        encoding="utf-8"
    ) == "первый\n"
    assert _read_manifest(session_root)["round"] == 1
