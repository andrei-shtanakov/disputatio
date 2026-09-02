"""RED: TASK-011 / BEH-11 — невалидный UTF-8 всплывает как SyntaxError с диагностикой.

`workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-11`: наружу должен
возбуждаться `SyntaxError` (не `UnicodeDecodeError`), сообщение которого содержит
путь проблемного файла, а `__cause__` — тот же экземпляр `UnicodeDecodeError`,
что возник при чтении. Сейчас `scan_package_purity` читает файлы через
`path.read_text(encoding="utf-8")` без обработки ошибок декодирования, поэтому
`UnicodeDecodeError` пролетает наружу как есть.
"""

import shutil
from pathlib import Path

import disputatio.core as _core_package
from disputatio.runtime import purity

_CORE_PACKAGE_NAME = "disputatio.core"


def _core_copy(tmp_path: Path) -> Path:
    """Временная копия дерева `core` — площадка для файла с плохими байтами."""
    core_file = _core_package.__file__
    assert core_file is not None, "у disputatio.core нет __file__"
    destination = tmp_path / "core"
    shutil.copytree(
        Path(core_file).parent,
        destination,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return destination


def test_scan_package_purity_wraps_unicode_decode_error(tmp_path: Path) -> None:
    package_dir = _core_copy(tmp_path)
    bad_file = package_dir / "broken_encoding.py"
    bad_file.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    try:
        purity.scan_package_purity(package_dir, package_name=_CORE_PACKAGE_NAME)
    except SyntaxError as exc:
        assert str(bad_file) in str(exc), (
            f"SyntaxError message should mention {bad_file}, got: {exc}"
        )
        assert isinstance(exc.__cause__, UnicodeDecodeError), (
            f"SyntaxError.__cause__ should be the UnicodeDecodeError, "
            f"got: {exc.__cause__!r}"
        )
    else:
        raise AssertionError(
            "scan_package_purity() should raise SyntaxError on invalid UTF-8, "
            "not propagate UnicodeDecodeError or succeed silently"
        )
