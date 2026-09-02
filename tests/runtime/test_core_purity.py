"""AST-скан чистоты ядра: TASK-001, [REQ-002], [DESIGN-002], [ADR-006].

Сканер живёт в `src/disputatio/runtime/purity.py`, а не под `tests/`
([ADR-006]): `commit_red` фиксирует весь `tests/`, поэтому анализатор внутри
тестов сделал бы red нечестным. На red-чекпоинте модуля ещё нет — импорт
обёрнут в `_purity()`, превращающий `ImportError` в `AssertionError`, иначе
гейт не принял бы падение.

Позитивная половина сканирует реальный `disputatio.core`; негативная —
временную копию того же дерева, куда кладётся по одному модулю на каждый вид
импорта. Без негативной половины тест был бы вакуумен: он зеленел бы на
сканере, всегда возвращающем `[]`.
"""

import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import disputatio.core as _core_package

_CORE_PACKAGE_NAME = "disputatio.core"


def _purity() -> ModuleType:
    """Модуль сканера; на red-фазе — `AssertionError` вместо `ImportError`."""
    try:
        from disputatio.runtime import purity
    except ImportError as exc:  # red-фаза: runtime/purity.py ещё не создан
        raise AssertionError("src/disputatio/runtime/purity.py ещё не создан") from exc
    return purity


def _real_core_dir() -> Path:
    """Каталог фактического пакета `disputatio.core`."""
    core_file = _core_package.__file__
    assert core_file is not None, "у disputatio.core нет __file__"
    return Path(core_file).parent


def _core_copy(tmp_path: Path) -> Path:
    """Временная копия дерева `core` — площадка для негативных кейсов."""
    destination = tmp_path / "core"
    shutil.copytree(
        _real_core_dir(),
        destination,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return destination


def _write_module(package_dir: Path, name: str, source: str) -> Path:
    """Кладёт модуль `name` с исходником `source` в копию дерева."""
    path = package_dir / name
    path.write_text(source, encoding="utf-8")
    return path


def _scan(package_dir: Path) -> list[Any]:
    """Скан `package_dir` как пакета `disputatio.core`."""
    return _purity().scan_package_purity(package_dir, package_name=_CORE_PACKAGE_NAME)


def _tuples(violations: list[Any]) -> list[tuple[Path, int, str, str]]:
    """Нарушения в виде кортежей `(module, lineno, imported, kind)`."""
    return [(v.module, v.lineno, v.imported, v.kind) for v in violations]


def test_real_core_package_scans_clean() -> None:
    """Позитив [REQ-002]: фактический `disputatio.core` нарушений не содержит."""
    assert _scan(_real_core_dir()) == []


def test_forbidden_roots_are_the_five_implementation_packages() -> None:
    """`FORBIDDEN_ROOTS` [DESIGN-002] — ровно пять пакетов-реализаций."""
    assert _purity().FORBIDDEN_ROOTS == frozenset(
        {
            "disputatio.events",
            "disputatio.adapters",
            "disputatio.verifier",
            "disputatio.context",
            "disputatio.runtime",
        }
    )


def test_plain_import_of_forbidden_root_is_reported(tmp_path: Path) -> None:
    """`import disputatio.events` в модуле ядра — нарушение вида `import`."""
    core = _core_copy(tmp_path)
    rogue = _write_module(core, "rogue_import.py", "import disputatio.events\n")

    assert _tuples(_scan(core)) == [(rogue, 1, "disputatio.events", "import")]


def test_from_import_of_forbidden_root_is_reported(tmp_path: Path) -> None:
    """`from disputatio.adapters import X` — нарушение вида `from`."""
    core = _core_copy(tmp_path)
    rogue = _write_module(
        core,
        "rogue_from.py",
        "from disputatio.adapters import ClaudeCodeAdapter\n",
    )

    assert _tuples(_scan(core)) == [(rogue, 1, "disputatio.adapters", "from")]


def test_relative_import_resolving_to_forbidden_root_is_reported(
    tmp_path: Path,
) -> None:
    """`level > 0` разрешается относительно пакета модуля, а не по тексту."""
    core = _core_copy(tmp_path)
    sub = core / "sub"
    sub.mkdir()
    _write_module(sub, "__init__.py", "")
    rogue = _write_module(sub, "rogue_relative.py", "from ...verifier import Gate\n")

    assert _tuples(_scan(core)) == [(rogue, 1, "disputatio.verifier", "from")]


def test_dunder_import_with_string_literal_is_reported(tmp_path: Path) -> None:
    """`__import__("disputatio.context")` — нарушение вида `dynamic-import`."""
    core = _core_copy(tmp_path)
    rogue = _write_module(
        core,
        "rogue_dunder.py",
        'module = __import__("disputatio.context")\n',
    )

    assert _tuples(_scan(core)) == [(rogue, 1, "disputatio.context", "dynamic-import")]


def test_importlib_import_module_with_string_literal_is_reported(
    tmp_path: Path,
) -> None:
    """`importlib.import_module("disputatio.runtime")` — тоже `dynamic-import`."""
    core = _core_copy(tmp_path)
    rogue = _write_module(
        core,
        "rogue_importlib.py",
        'import importlib\n\nmodule = importlib.import_module("disputatio.runtime")\n',
    )

    assert _tuples(_scan(core)) == [(rogue, 3, "disputatio.runtime", "dynamic-import")]


def test_neighbouring_package_with_shared_prefix_is_not_a_violation(
    tmp_path: Path,
) -> None:
    """Граница имени: `disputatio.eventsX` — другой пакет, не нарушение."""
    core = _core_copy(tmp_path)
    _write_module(
        core,
        "neighbour.py",
        "import disputatio.eventsX\nfrom disputatio.contextual import helper\n",
    )

    assert _scan(core) == []


def test_submodule_of_forbidden_root_is_a_violation(tmp_path: Path) -> None:
    """Граница имени: `disputatio.events.paths` лежит под запрещённым корнем."""
    core = _core_copy(tmp_path)
    rogue = _write_module(core, "deep_import.py", "import disputatio.events.paths\n")

    assert _tuples(_scan(core)) == [(rogue, 1, "disputatio.events.paths", "import")]


def test_dynamic_import_with_non_constant_argument_is_reported(
    tmp_path: Path,
) -> None:
    """Статически неразрешимый импорт внутри ядра — нарушение сам по себе."""
    core = _core_copy(tmp_path)
    rogue = _write_module(
        core,
        "rogue_dynamic.py",
        "import importlib\n\n\ndef load(name: str):\n"
        "    return importlib.import_module(name)\n",
    )

    violations = _scan(core)

    assert len(violations) == 1, violations
    assert violations[0].module == rogue
    assert violations[0].lineno == 5
    assert violations[0].kind == "dynamic-import"
    assert violations[0].imported != ""


def test_violation_exposes_module_lineno_imported_and_kind(tmp_path: Path) -> None:
    """`PurityViolation` несёт `module`, `lineno`, `imported`, `kind`."""
    purity = _purity()
    core = _core_copy(tmp_path)
    rogue = _write_module(
        core,
        "rogue_fields.py",
        '"""Модуль-нарушитель."""\n\nimport disputatio.events\n',
    )

    (violation,) = _scan(core)

    assert isinstance(violation, purity.PurityViolation)
    assert violation.module == rogue
    assert violation.lineno == 3
    assert violation.imported == "disputatio.events"
    assert violation.kind == "import"


def test_scan_package_purity_wraps_unicode_decode_error(tmp_path: Path) -> None:
    """BEH-11 [FR-10, FR-11]: невалидный UTF-8 всплывает как `SyntaxError`."""
    core = _core_copy(tmp_path)
    bad_file = _write_module(core, "broken_encoding.py", "")
    bad_file.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    try:
        _scan(core)
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


def test_forbidden_collection_is_a_parameter(tmp_path: Path) -> None:
    """Набор запрещённых корней — параметр, а не захардкоженная константа."""
    core = _core_copy(tmp_path)
    _write_module(core, "rogue_import.py", "import disputatio.events\n")

    violations = _purity().scan_package_purity(
        core,
        package_name=_CORE_PACKAGE_NAME,
        forbidden=("disputatio.adapters",),
    )

    assert violations == []


def test_scan_package_purity_fails_atomically_on_invalid_utf8(tmp_path) -> None:
    """BEH-12: ошибка декодирования атомарно прекращает весь scan
    (waiver TASK-012: атомарность дана по построению TASK-011)."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "dirty.py").write_text("import disputatio.adapters\n")
    (pkg / "broken.py").write_bytes(b"x = '\xff\xfe broken'\n")

    with pytest.raises(SyntaxError) as exc_info:
        _purity().scan_package_purity(
            pkg, package_name="pkg", forbidden=("disputatio.adapters",)
        )

    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)
