"""AST-скан чистоты ядра — машинная проверка INV-11 ([REQ-002], [DESIGN-002]).

Сканер отвечает на один вопрос: содержит ли дерево `src/disputatio/core/**`
импорт, разрешающийся в реализацию (`events`, `adapters`, `verifier`,
`context`, `runtime`)? Разбираются `ast.Import`, `ast.ImportFrom` (включая
относительные — уровень `level > 0` разрешается относительно пакета модуля),
а также вызовы `__import__(...)` и `importlib.import_module(...)`.

Модуль живёт в `src`, а не в `tests` ([ADR-006]): граница INV-11 — часть
архитектуры пакета, а не тестовая утилита, и анализатор внутри `tests/`
сделал бы red-чекпоинт нечестным.
"""

import ast
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_ROOTS: frozenset[str] = frozenset(
    {
        "disputatio.events",
        "disputatio.adapters",
        "disputatio.verifier",
        "disputatio.context",
        "disputatio.runtime",
    }
)

DYNAMIC_IMPORT_CALLS: frozenset[str] = frozenset({"__import__", "import_module"})

KIND_IMPORT = "import"
KIND_FROM = "from"
KIND_DYNAMIC = "dynamic-import"


@dataclass(frozen=True, slots=True)
class PurityViolation:
    """Одно нарушение INV-11: модуль, строка, разрешённое имя импорта."""

    module: Path
    lineno: int
    imported: str
    kind: str


def scan_package_purity(
    package_dir: Path,
    *,
    package_name: str,
    forbidden: Collection[str] = FORBIDDEN_ROOTS,
) -> list[PurityViolation]:
    """Возвращает все импорты `package_dir`, разрешающиеся в `forbidden`.

    Совпадение — по границе имени пакета: `disputatio.eventsX` не считается
    нарушением, `disputatio.events.paths` — считается ([REQ-002]). Обход
    рекурсивный и файловый, поэтому новый модуль ядра попадает под проверку
    без правок сканера. Пути модулей возвращаются как есть, без `resolve()`,
    чтобы совпадать с переданным `package_dir`.
    """
    violations: list[PurityViolation] = []
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = _scan_tree(
            tree,
            module=path,
            package=_module_package(path, package_dir, package_name),
            forbidden=forbidden,
        )
        violations.extend(sorted(found, key=lambda violation: violation.lineno))
    return violations


def _module_package(path: Path, package_dir: Path, package_name: str) -> str:
    """Пакет, относительно которого разрешаются относительные импорты `path`."""
    parent_parts = path.relative_to(package_dir).parts[:-1]
    return ".".join((package_name, *parent_parts))


def _scan_tree(
    tree: ast.AST,
    *,
    module: Path,
    package: str,
    forbidden: Collection[str],
) -> Iterator[PurityViolation]:
    """Нарушения одного разобранного модуля, в порядке обхода AST."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from _scan_import(node, module=module, forbidden=forbidden)
        elif isinstance(node, ast.ImportFrom):
            yield from _scan_import_from(
                node, module=module, package=package, forbidden=forbidden
            )
        elif isinstance(node, ast.Call):
            yield from _scan_call(node, module=module, forbidden=forbidden)


def _scan_import(
    node: ast.Import, *, module: Path, forbidden: Collection[str]
) -> Iterator[PurityViolation]:
    """`import a.b.c` — имя алиаса и есть разрешённое имя модуля."""
    for alias in node.names:
        if _is_forbidden(alias.name, forbidden):
            yield PurityViolation(module, node.lineno, alias.name, KIND_IMPORT)


def _scan_import_from(
    node: ast.ImportFrom,
    *,
    module: Path,
    package: str,
    forbidden: Collection[str],
) -> Iterator[PurityViolation]:
    """`from X import a, b` — нарушением может быть и `X`, и `X.a`."""
    base = (
        _resolve_relative(package, node.level, node.module)
        if node.level
        else (node.module or "")
    )
    if _is_forbidden(base, forbidden):
        yield PurityViolation(module, node.lineno, base, KIND_FROM)
        return
    for alias in node.names:
        candidate = f"{base}.{alias.name}" if base else alias.name
        if _is_forbidden(candidate, forbidden):
            yield PurityViolation(module, node.lineno, candidate, KIND_FROM)


def _scan_call(
    node: ast.Call, *, module: Path, forbidden: Collection[str]
) -> Iterator[PurityViolation]:
    """`__import__`/`import_module`: литерал сверяется, неконстанта — нарушение."""
    if _called_name(node.func) not in DYNAMIC_IMPORT_CALLS:
        return
    argument = node.args[0] if node.args else None
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        if _is_forbidden(argument.value, forbidden):
            yield PurityViolation(module, node.lineno, argument.value, KIND_DYNAMIC)
        return
    yield PurityViolation(module, node.lineno, ast.unparse(node), KIND_DYNAMIC)


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    """Абсолютное имя относительного импорта уровня `level` внутри `package`."""
    base_parts = package.split(".") if package else []
    kept_count = len(base_parts) - (level - 1)
    kept = base_parts[:kept_count] if kept_count > 0 else []
    return ".".join((*kept, module)) if module else ".".join(kept)


def _called_name(func: ast.expr) -> str:
    """Имя вызываемого: `import_module` и для `Name`, и для `Attribute`."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_forbidden(name: str, forbidden: Collection[str]) -> bool:
    """`True`, если `name` — сам запрещённый корень или его под-модуль."""
    return any(name == root or name.startswith(f"{root}.") for root in forbidden)
