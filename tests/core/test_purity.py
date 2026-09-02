"""Тесты чистоты ядра: TASK-010, [DESIGN-009], [REQ-005], [NFR-003].

Чекер (`.purity_checker`) обходит модули пакета через `ast.parse` их
исходников — читает pytest, не ядро. Импорт чекера оборачивается: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне теста сломал бы collection.
Red-селектор (`test_check_purity_dynamically_discovers_new_module_with_bad_import`)
превращает `ImportError` в `AssertionError` — гейт принимает red только при
падении assertion'ом.
"""

import ast
import dataclasses
import inspect
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


def test_core_import_boundary() -> None:
    """BEH-14: сигнатуры/структуры и dependency-boundary `disputatio.core`.

    В отличие от прочих тестов модуля, проверяет запрет зависимостей не
    локальным allowlist-чекером (`.purity_checker`, TASK-010), а каноническим
    AST-сканером `disputatio.runtime.purity.scan_package_purity`, которым
    реально пользуется остальной проект ([REQ-002], [DESIGN-002]) — у
    `.purity_checker` нет понятия о `FORBIDDEN_ROOTS`/`PurityViolation`.
    """
    from disputatio.core.oscillation import (
        CLAIM_SIMILARITY_THRESHOLD,
        OSCILLATION_DIFF_THRESHOLD,
        _changed_lines,
        patch_similarity,
    )
    from disputatio.runtime.purity import (
        FORBIDDEN_ROOTS,
        PurityViolation,
        scan_package_purity,
    )

    checker = _checker()

    changed_lines_sig = inspect.signature(_changed_lines)
    assert list(changed_lines_sig.parameters) == ["patch"]
    assert changed_lines_sig.parameters["patch"].annotation is str
    assert changed_lines_sig.return_annotation == set[str]

    patch_similarity_sig = inspect.signature(patch_similarity)
    assert list(patch_similarity_sig.parameters) == ["a", "b"]
    assert patch_similarity_sig.parameters["a"].annotation is str
    assert patch_similarity_sig.parameters["b"].annotation is str
    assert patch_similarity_sig.return_annotation is float

    scan_sig = inspect.signature(scan_package_purity)
    assert list(scan_sig.parameters) == ["package_dir", "package_name", "forbidden"]
    assert (
        scan_sig.parameters["package_dir"].kind
        is inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert scan_sig.parameters["package_name"].kind is inspect.Parameter.KEYWORD_ONLY
    assert scan_sig.parameters["forbidden"].kind is inspect.Parameter.KEYWORD_ONLY
    assert scan_sig.parameters["forbidden"].default == FORBIDDEN_ROOTS
    assert scan_sig.return_annotation == list[PurityViolation]

    assert [field.name for field in dataclasses.fields(PurityViolation)] == [
        "module",
        "lineno",
        "imported",
        "kind",
    ]

    assert OSCILLATION_DIFF_THRESHOLD == 0.8
    assert CLAIM_SIMILARITY_THRESHOLD == 0.7

    assert FORBIDDEN_ROOTS == frozenset(
        {
            "disputatio.events",
            "disputatio.adapters",
            "disputatio.verifier",
            "disputatio.context",
            "disputatio.runtime",
        }
    )

    assert (
        scan_package_purity(checker.core_package_dir(), package_name="disputatio.core")
        == []
    )
