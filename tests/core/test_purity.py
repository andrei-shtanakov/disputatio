"""Тесты чистоты ядра: TASK-010, [DESIGN-009], [REQ-005], [NFR-003].

Чекер (`.purity_checker`) обходит модули пакета через `ast.parse` их
исходников — читает pytest, не ядро. Импорт чекера оборачивается: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне теста сломал бы collection.
Red-селектор (`test_check_purity_dynamically_discovers_new_module_with_bad_import`)
превращает `ImportError` в `AssertionError` — гейт принимает red только при
падении assertion'ом.
"""

import ast
from pathlib import Path
from types import ModuleType


def _checker() -> ModuleType:
    try:
        from . import purity_checker
    except ImportError as exc:  # red-фаза: purity_checker.py ещё не создан
        raise AssertionError("tests/core/purity_checker.py ещё не создан") from exc
    return purity_checker


def test_check_purity_dynamically_discovers_new_module_with_bad_import(
    tmp_path: Path,
) -> None:
    """Новый модуль каталога не выпадает из обхода; нарушение назван по имени."""
    checker = _checker()

    (tmp_path / "rogue.py").write_text("import subprocess\n", encoding="utf-8")
    (tmp_path / "clean.py").write_text("from typing import Final\n", encoding="utf-8")

    violations = checker.check_purity(tmp_path)

    assert violations == {"rogue.py": ["subprocess"]}


def test_check_purity_flags_multiple_violations_by_name(tmp_path: Path) -> None:
    """Несколько нарушений в одном модуле — все имена в отчёте, по порядку."""
    checker = _checker()

    (tmp_path / "bad.py").write_text(
        "import os\n\n\ndef f():\n    return open('x')\n", encoding="utf-8"
    )

    violations = checker.check_purity(tmp_path)

    assert violations == {"bad.py": ["os", "open"]}


def test_scan_source_allows_allowlisted_imports() -> None:
    """Allowlist [REQ-005]: stdlib-логика + `disputatio.contracts`/`disputatio.core`."""
    checker = _checker()

    source = (
        "import dataclasses\n"
        "import enum\n"
        "import typing\n"
        "import difflib\n"
        "import datetime\n"
        "from collections.abc import Mapping\n"
        "from disputatio.contracts import SessionPhase\n"
        "from disputatio.core.transitions import check_transition\n"
    )

    assert checker.scan_source(source) == []


def test_scan_source_detects_forbidden_import_and_call() -> None:
    """Негативный самотест чекера: запрещённый импорт и вызов в синтетическом AST."""
    checker = _checker()

    source = "import socket\n\n\ndef f():\n    return open('x')\n"

    assert checker.scan_source(source) == ["socket", "open"]


def test_scan_source_detects_datetime_now_and_today_calls() -> None:
    """`datetime.now()`/`datetime.today()` — запрещённые недетерминированные часы."""
    checker = _checker()

    source = (
        "from datetime import datetime\n\n\n"
        "def f():\n    return datetime.now(), datetime.today()\n"
    )

    assert checker.scan_source(source) == ["datetime.now", "datetime.today"]


def test_scan_source_rejects_relative_imports() -> None:
    """Относительный импорт (`from . import x`) — вне allowlist'а, всегда нарушение."""
    checker = _checker()

    source = "from . import sibling\n"

    assert checker.scan_source(source) == [".sibling"]


def test_actual_core_package_has_no_purity_violations() -> None:
    """Green [DESIGN-009]: фактический `disputatio.core` проходит чекер чисто."""
    checker = _checker()

    assert checker.check_purity(checker.core_package_dir()) == {}


def test_actual_core_modules_never_call_open_or_datetime_now_today() -> None:
    """[REQ-005]: в ядре нет `open(...)` и `datetime.now()`/`datetime.today()`."""
    checker = _checker()

    for path in checker.iter_module_paths(checker.core_package_dir()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert checker.find_forbidden_calls(tree) == [], path.name
