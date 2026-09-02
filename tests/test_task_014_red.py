"""RED: TASK-014 / BEH-14 — граница зависимостей `disputatio.core` без канонической проверки.

`workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-14`: после того как
TASK-011–013 закрыли обработку невалидного UTF-8 в `scan_package_purity`, нужно
подтвердить, что публичные сигнатуры (`_changed_lines`, `patch_similarity`,
`scan_package_purity`, структура `PurityViolation`) и запрет зависимостей
`disputatio.core` от внешних слоёв (`disputatio.events`/`adapters`/`verifier`/
`context`/`runtime`) сохранены — причём именно через канонический
AST-сканер `disputatio.runtime.purity.scan_package_purity`/`PurityViolation`
(`FORBIDDEN_ROOTS`), а не только через локальный allowlist-чекер
`tests/core/purity_checker.py` (TASK-010), у которого нет понятия о
`FORBIDDEN_ROOTS` и `PurityViolation`.

Checked_by BEH-14 — `tests/core/test_purity.py::test_core_import_boundary`.
Этой проверки в модуле сейчас нет: `tests/core/test_purity.py` использует
только `.purity_checker`, ни разу не импортируя `disputatio.runtime.purity`.

Модуль читается как исходник (`ast.parse`), а не импортируется под именем
`tests.core.test_purity`: у `tests/` нет `__init__.py` верхнего уровня, и
пакетного dotted-импорта тестового кода в проекте нигде больше нет — синтетический
`importlib.import_module` уронил бы тест `ModuleNotFoundError` вместо честного
`AssertionError` о недостающем поведении.
"""

import ast
from pathlib import Path

_TEST_PURITY_MODULE = Path(__file__).parent / "core" / "test_purity.py"


def test_core_import_boundary_check_is_present_in_test_purity_module() -> None:
    tree = ast.parse(_TEST_PURITY_MODULE.read_text(encoding="utf-8"))
    defined_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    assert "test_core_import_boundary" in defined_names, (
        "tests/core/test_purity.py должен содержать test_core_import_boundary "
        "(BEH-14): каноническая проверка dependency-boundary disputatio.core "
        "через disputatio.runtime.purity.scan_package_purity/PurityViolation "
        f"ещё не добавлена в модуль, определены: {sorted(defined_names)}"
    )
