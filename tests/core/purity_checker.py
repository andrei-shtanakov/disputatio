"""Чекер чистоты `disputatio.core` — allowlist импортов + запрет часов/I-O.

DESIGN-009/[REQ-005]: `disputatio.core` вправе опираться только на
перечисленную stdlib-логику и `disputatio.contracts` (плюс сами себя —
модули пакета ссылаются друг на друга); всё прочее — потенциальная утечка
I/O в FSM, которая должна быть тестируема на чистых фейках [NFR-003]. Этот
модуль живёт под `tests/`, поэтому ему, в отличие от ядра, разрешён
файловый I/O — но только read-only чтение исходников для `ast.parse`,
никакой записи.
"""

import ast
from pathlib import Path

import disputatio.core as _core_package

ALLOWED_EXACT_MODULES: frozenset[str] = frozenset(
    {"dataclasses", "enum", "typing", "difflib", "datetime"}
)
ALLOWED_MODULE_PREFIXES: frozenset[str] = frozenset(
    {"collections.abc", "disputatio.contracts", "disputatio.core"}
)
FORBIDDEN_CALL_NAMES: frozenset[str] = frozenset({"open"})
FORBIDDEN_DATETIME_ATTRS: frozenset[str] = frozenset({"now", "today"})


def core_package_dir() -> Path:
    """Каталог пакета `disputatio.core` — точка входа динамического обхода."""
    return Path(_core_package.__file__).resolve().parent


def iter_module_paths(package_dir: Path) -> list[Path]:
    """Все `*.py`-файлы каталога `package_dir`, отсортированные по имени.

    Обход файловой системы, а не статический список имён — новый модуль
    пакета попадает в проверку без правки чекера.
    """
    return sorted(package_dir.glob("*.py"))


def _is_allowed_module(module_name: str) -> bool:
    """`True`, если `module_name` — точное совпадение либо под-модуль allowlist'а."""
    if module_name in ALLOWED_EXACT_MODULES:
        return True
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in ALLOWED_MODULE_PREFIXES
    )


def find_disallowed_imports(tree: ast.AST) -> list[str]:
    """Импорты дерева `tree` вне allowlist'а, по имени модуля/импорта."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(
                alias.name for alias in node.names if not _is_allowed_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = f"{'.' * node.level}{node.module or ''}"
            if node.level == 0 and _is_allowed_module(module):
                continue
            if node.module is None:  # `from . import x` — module пуст, есть alias'ы
                violations.extend(
                    f"{'.' * node.level}{alias.name}" for alias in node.names
                )
            else:
                violations.append(module)
    return violations


def find_forbidden_calls(tree: ast.AST) -> list[str]:
    """Вызовы `open(...)` и `datetime.now()`/`datetime.today()` в дереве `tree`."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALL_NAMES:
            violations.append(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_CALL_NAMES:
            violations.append(func.attr)
        elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_DATETIME_ATTRS:
            violations.append(f"datetime.{func.attr}")
    return violations


def scan_source(source: str) -> list[str]:
    """Все нарушения (запрещённые импорты + вызовы) исходника `source`."""
    tree = ast.parse(source)
    return [*find_disallowed_imports(tree), *find_forbidden_calls(tree)]


def check_purity(package_dir: Path) -> dict[str, list[str]]:
    """Нарушения по модулям каталога `package_dir`; пустой `dict` — чисто."""
    violations: dict[str, list[str]] = {}
    for path in iter_module_paths(package_dir):
        found = scan_source(path.read_text(encoding="utf-8"))
        if found:
            violations[path.name] = found
    return violations
