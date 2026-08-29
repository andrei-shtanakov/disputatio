"""Тесты `__init__.py` — публичный API пакета `disputatio.context`: TASK-006.

Пакет существует с TASK-001 (пустой модуль с докстрингом), поэтому импорт
самого пакета безопасен и на red-чекпоинте: тесты падают `AssertionError`
на отсутствующем атрибуте, а не `ImportError` на коллекте. Отсюда же
`importlib` + `getattr` вместо `from ... import ...` на уровне модуля.

Публичных имён ровно четыре: пара develop/analyze-раунда
(`build_author_prompt`, `build_reviewer_prompt`, [DESIGN-001]) и пара
doc-раунда пайплайна (`build_doc_author_prompt`,
`build_doc_reviewer_prompt`, §5.1–§5.2 SPEC-002). Doc-пара стала публичной
вместе с интеграцией пайплайна (задача 17): шаг цикла выбирает сборщик по
режиму сессии, а импорт подмодуля из `runtime` обходил бы ту же границу,
которая закрыта для develop-пары.

Всё остальное — `tags`, `sections`, `schema_rules` и их функции — деталь
реализации: оркестратор завязывается на четыре точки входа, а не на
внутреннюю раскладку модулей (NFR-003). Поэтому «ровно четыре» пинится с
обеих сторон: имена присутствуют и тождественны оригиналам, и ничего сверх
них наружу не выходит.
"""

import importlib
from typing import Final

PACKAGE: Final = "disputatio.context"

# Публичное имя → подмодуль, в котором оно определено. Реэкспорт обязан
# быть тем же объектом, а не копией или тенью.
PUBLIC_API: Final[dict[str, str]] = {
    "build_author_prompt": "disputatio.context.author",
    "build_doc_author_prompt": "disputatio.context.doc_author",
    "build_doc_reviewer_prompt": "disputatio.context.doc_reviewer",
    "build_reviewer_prompt": "disputatio.context.reviewer",
}

# Имена, которых в публичном API быть НЕ должно: внутренние хелперы,
# статические константы секций и сами подмодули. Список поимённый, а не
# по эвристике префикса, — он и есть формулировка «остаётся деталью
# реализации».
INTERNAL_NAMES: Final[tuple[str, ...]] = (
    "REVIEW_SCHEMA_REQUIREMENTS",
    "CHECKLIST_TITLE",
    "DOCUMENTS_TITLE",
    "DOC_PATHS_TITLE",
    "ADOPTED_FINDINGS_TITLE",
    "MATERIALS_TITLE",
    "TASK_TITLE",
    "author",
    "doc_author",
    "doc_reviewer",
    "reviewer",
    "schema_rules",
    "sections",
    "tags",
    "_check_prior_round",
    "_check_round",
    "render_directive_section",
    "render_failed_gates_section",
    "render_issues_section",
    "render_task_section",
    "render_verification_section",
    "select_failed_gates",
    "select_open_issues",
    "select_resolved_issues",
    "wrap_artifact_data",
)


def _star_import_names() -> set[str]:
    """Имена, которые получает `from disputatio.context import *`."""
    namespace: dict[str, object] = {}
    # `from ... import *` — синтаксис только уровня модуля, а воспроизводить
    # его семантику вручную значило бы проверять свою же реконструкцию.
    exec(f"from {PACKAGE} import *", namespace)  # noqa: S102
    return {name for name in namespace if not name.startswith("__")}


def test_public_functions_are_submodule_objects() -> None:
    """Обе функции доступны из пакета и тождественны оригиналам."""
    package = importlib.import_module(PACKAGE)
    mismatched = [
        name
        for name, module_name in PUBLIC_API.items()
        if getattr(package, name, None)
        is not getattr(importlib.import_module(module_name), name)
    ]
    assert mismatched == []


def test_all_lists_exactly_the_two_public_names() -> None:
    """`__all__` объявлен и содержит ровно эти два имени, без дубликатов."""
    package = importlib.import_module(PACKAGE)
    exported = getattr(package, "__all__", None)
    assert exported is not None
    assert sorted(exported) == sorted(PUBLIC_API)
    assert len(exported) == len(set(exported))


def test_star_import_exposes_exactly_the_public_api() -> None:
    """Звёздочный импорт отдаёт публичный API и ничего сверх него."""
    assert _star_import_names() == set(PUBLIC_API)


def test_star_import_hides_internal_helpers() -> None:
    """Ни один внутренний хелпер и ни один подмодуль не протаскивается."""
    names = _star_import_names()
    assert sorted(names & set(INTERNAL_NAMES)) == []
    assert names >= set(PUBLIC_API)
