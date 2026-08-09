"""Тесты __init__.py — публичный API ядра: TASK-011, [DESIGN-010].

Пакет `disputatio.core` существует с самого начала (докстринг), поэтому его
импорт на уровне модуля безопасен и на red-чекпоинте. Red-селектор
(`test_public_names_importable_from_package`) падает assertion'ом, пока
реэкспорт не написан: `hasattr` возвращает False без ImportError.
Потребители волны 2 (w-runtime) импортируют только из `disputatio.core`, не
из подмодулей — тесты фиксируют состав и сортировку `__all__`, тождественность
реэкспортированных объектов оригиналам в подмодулях, и наличие docstring'ов
у публичных классов/функций [NFR-002].
"""

import importlib
import inspect
from typing import Final

from disputatio import core

# Публичные имена по подмодулям — источник ожиданий для всех тестов ниже.
EXPECTED_BY_MODULE: Final[dict[str, tuple[str, ...]]] = {
    "transitions": (
        "InvalidTransition",
        "TRANSITIONS",
        "TERMINAL_PHASES",
        "check_transition",
    ),
    "writers": ("Writer", "ACTIVE_WRITER", "active_writer"),
    "deciding": (
        "DecidingInputs",
        "DecisionDraft",
        "decide",
        "REASON_CONVERGED",
        "REASON_ANTI_SYCOPHANCY",
        "REASON_BUDGET_TOKENS",
        "REASON_BUDGET_WALL",
        "REASON_OSCILLATION_DIFF",
        "REASON_OSCILLATION_ISSUE",
        "REASON_MAX_ROUNDS",
        "REASON_CONTINUE",
    ),
    "oscillation": (
        "OSCILLATION_DIFF_THRESHOLD",
        "CLAIM_SIMILARITY_THRESHOLD",
        "patch_similarity",
        "find_repeated_issue",
    ),
    "machine": ("SessionFsm", "RetryAction", "is_partial"),
}

EXPECTED_PUBLIC_NAMES: Final[tuple[str, ...]] = tuple(
    name for names in EXPECTED_BY_MODULE.values() for name in names
)


def test_public_names_importable_from_package() -> None:
    """Каждое публичное имя доступно как атрибут `disputatio.core`."""
    missing = [name for name in EXPECTED_PUBLIC_NAMES if not hasattr(core, name)]
    assert missing == []


def test_all_sorted_and_consistent_with_reexports() -> None:
    """`__all__` без дубликатов, отсортирован и поимённо совпадает с реэкспортом."""
    exported = getattr(core, "__all__", None)
    assert exported is not None
    assert len(exported) == len(set(exported))
    assert exported == sorted(exported)
    assert set(exported) == set(EXPECTED_PUBLIC_NAMES)


def test_reexports_are_submodule_objects() -> None:
    """Реэкспорт — те же объекты, что в подмодулях (не копии и не тени)."""
    mismatched = [
        name
        for module_name, names in EXPECTED_BY_MODULE.items()
        for name in names
        if getattr(core, name, None)
        is not getattr(importlib.import_module(f"disputatio.core.{module_name}"), name)
    ]
    assert mismatched == []


def test_public_classes_and_functions_have_docstrings() -> None:
    """Каждый публичный класс/функция из `__all__` документирован [NFR-002]."""
    undocumented = [
        name
        for name in EXPECTED_PUBLIC_NAMES
        if (
            inspect.isclass(getattr(core, name, None))
            or inspect.isfunction(getattr(core, name, None))
        )
        and not (getattr(core, name).__doc__ or "").strip()
    ]
    assert undocumented == []
