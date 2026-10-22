"""Сторонние импорты `src/disputatio` объявлены в `[project] dependencies`.

Регрессия #159: `anyio` импортировался `cli.py` и `runtime/composition.py`,
но жил только в `[dependency-groups] dev` — установка без dev-групп
(`uv sync --no-default-groups`, инсталлятор DarkFactory) падала на
`disp --help` с `ModuleNotFoundError`. Suite этого не видел: тесты крутятся в
окружении с dev-группой, где `anyio` есть.

Поэтому проверяется объявление, а не окружение: импорты собираются `ast` по
всем модулям пакета (включая ленивые — внутри функций), стандартная
библиотека отсекается `sys.stdlib_module_names`, остальное переводится в имя
дистрибутива через `importlib.metadata.packages_distributions` и сверяется с
`[project] dependencies`.
"""

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
PACKAGE_DIR: Final[Path] = REPO_ROOT / "src" / "disputatio"
OWN_PACKAGE: Final[str] = "disputatio"


def _normalize(name: str) -> str:
    """Имя дистрибутива по PEP 503: регистр и `-_.` не различаются."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_runtime_dists() -> set[str]:
    """Нормализованные имена из `[project] dependencies`."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    return {
        _normalize(re.split(r"[\s<>=!~;\[(]", spec, maxsplit=1)[0])
        for spec in project["dependencies"]
    }


def _top_level_imports(source: str) -> set[str]:
    """Корневые имена абсолютных импортов модуля."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _third_party_imports() -> dict[str, set[Path]]:
    """Сторонние корневые импорты пакета → файлы, где они встречаются."""
    found: dict[str, set[Path]] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        for name in _top_level_imports(path.read_text()):
            if name == OWN_PACKAGE or name in sys.stdlib_module_names:
                continue
            found.setdefault(name, set()).add(path.relative_to(REPO_ROOT))
    return found


def test_scan_sees_known_third_party_imports() -> None:
    """Сканер не вакуумен: известные сторонние импорты он находит."""
    assert {"anyio", "pydantic", "yaml"} <= set(_third_party_imports())


def test_every_third_party_import_is_a_declared_runtime_dependency() -> None:
    """Каждый сторонний импорт покрыт `[project] dependencies`, не dev-группой."""
    declared = _declared_runtime_dists()
    dists_of = packages_distributions()
    undeclared = {
        name: sorted(str(p) for p in files)
        for name, files in _third_party_imports().items()
        if not {_normalize(d) for d in dists_of.get(name, [name])} & declared
    }
    assert undeclared == {}, f"импорты без runtime-зависимости: {undeclared}"
