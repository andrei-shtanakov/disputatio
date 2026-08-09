"""Статический сканер инварианта «адаптеры не исполняют gates» ([DESIGN-009]).

Не тестовый модуль (pytest его не собирает) — набор чистых функций, которые
`test_no_verification_gates.py` применяет к исходникам `disputatio.adapters`.
Читаются именно тексты модулей: сканер ничего из пакета не исполняет.

Известное ограничение, принимаемое сознательно (см. [DESIGN-009] и прецедент
ast-scan из w-fsm TASK-003): проверка чисто синтаксическая и обходится
`__import__`/`importlib.import_module`, `getattr`-склейкой имени и сборкой
argv из переменной вместо литерала. Здесь эти дыры не закрываются: acceptance
REQ-010 требует точного import-инварианта, а argv-проверка объявлена
эвристической.
"""

import ast
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from disputatio import adapters

VERIFIER_NAME = "Verifier"
"""Имя протокола, которое пакету адаптеров запрещено втягивать."""

VERIFIER_SOURCE_MODULES: tuple[str, ...] = (
    "disputatio.contracts",
    "disputatio.contracts.ports",
)
"""`ports` и re-export пакета `contracts` — оба пути к `Verifier`."""

GATE_BINARIES: tuple[str, ...] = ("pytest", "ruff")
"""Бинари детерминированных проверок; их запуск — зона `Verifier`/w-verifier."""

EXEMPT_ASSIGNMENT_NAMES: frozenset[str] = frozenset({"REVIEWER_DEFAULT_ALLOWED_TOOLS"})
"""Литералы этих констант — permission-набор для CLI, а не argv вызова."""


def adapters_package_dir() -> Path:
    """Каталог пакета `disputatio.adapters` (без зависимости от CWD)."""
    return Path(adapters.__file__).parent


def iter_module_paths(directory: Path) -> list[Path]:
    """Все `*.py` каталога, рекурсивно и в детерминированном порядке."""
    return sorted(directory.rglob("*.py"))


def find_verifier_usage(source: str) -> list[str]:
    """Ссылки на `Verifier` в исходнике: импорты и доступ через атрибут."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module in VERIFIER_SOURCE_MODULES:
            if any(alias.name == VERIFIER_NAME for alias in node.names):
                found.add(f"{node.module}.{VERIFIER_NAME}")
        elif isinstance(node, ast.Attribute) and node.attr == VERIFIER_NAME:
            found.add(ast.unparse(node))
    return sorted(found)


def find_gate_argv(source: str) -> list[str]:
    """Имена гейтов, захардкоженные как `argv[0]` (эвристика, см. docstring модуля)."""
    tree = ast.parse(source)
    exempt = _exempt_literal_ids(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple):
            first = node.elts[0] if node.elts and id(node) not in exempt else None
        elif isinstance(node, ast.Call):
            first = node.args[0] if node.args else None
        else:
            continue
        binary = _gate_binary_name(first)
        if binary is not None:
            found.add(binary)
    return sorted(found)


def scan_verifier_usage(directory: Path) -> dict[str, list[str]]:
    """`{относительный путь модуля: ссылки на Verifier}` — пусто, если чисто."""
    return _scan(directory, find_verifier_usage)


def scan_gate_argv(directory: Path) -> dict[str, list[str]]:
    """`{относительный путь модуля: имена гейтов в argv[0]}` — пусто, если чисто."""
    return _scan(directory, find_gate_argv)


def _scan(directory: Path, finder: Callable[[str], list[str]]) -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for path in iter_module_paths(directory):
        found = finder(path.read_text(encoding="utf-8"))
        if found:
            violations[path.relative_to(directory).as_posix()] = found
    return violations


def _exempt_literal_ids(tree: ast.AST) -> set[int]:
    """`id()` литералов, присвоенных освобождённым константам-allowlist'ам."""
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is not None and any(
            isinstance(target, ast.Name) and target.id in EXEMPT_ASSIGNMENT_NAMES
            for target in targets
        ):
            exempt.add(id(node.value))
    return exempt


def _gate_binary_name(node: ast.expr | None) -> str | None:
    """Имя гейта, если узел — строковый литерал вида `pytest`/`/usr/bin/ruff`."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    binary = PurePosixPath(node.value).name
    return binary if binary in GATE_BINARIES else None
