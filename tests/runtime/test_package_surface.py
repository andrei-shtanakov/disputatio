"""Публичная поверхность `disputatio/__init__.py`: TASK-023, [REQ-022], [DESIGN-022].

Корневой `__init__` намеренно беден: он несёт `__version__` и явный `__all__`,
но не реэкспортирует ни `runtime`, ни `cli` — `import disputatio` не должен
тянуть `anyio`, адаптеры и subprocess.

Две ловушки, из-за которых часть проверок вынесена в подпроцесс и записана
через **точное равенство**:

* «`from disputatio import *` не протаскивает лишнего» без сравнения на
  равенство вакуумно: пока `__all__` не определён, звёздочный импорт молча
  отдаёт пустое множество (единственное публичное имя модуля — `__version__`
  — начинается с подчёркивания и под звёздочку не попадает), и любое
  «⊆ ожидаемого» проходит на пустом `__init__`. Поэтому селектор требует
  непустой `__all__` и равенства множеств.
* `hasattr(disputatio, "runtime")` внутри suite ничего не доказывает: любой
  сосед по `tests/runtime/` уже сделал `import disputatio.runtime`, а импорт
  подмодуля выставляет его атрибутом пакета. Настоящее «имена подмодулей
  наружу не протаскиваются» видно только в чистом процессе — там же
  проверяется и `sys.modules`.
"""

import json
import subprocess
import sys
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Final

import disputatio

#: Корень репозитория: tests/runtime/<файл> → parents[2].
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Подмодули пакета — ни одно из этих имён не должно быть атрибутом
#: `disputatio` после голого `import disputatio`.
SUBMODULES: Final[tuple[str, ...]] = (
    "adapters",
    "cli",
    "context",
    "contracts",
    "core",
    "events",
    "runtime",
    "verifier",
)

#: Модули, которых голый `import disputatio` втягивать не имеет права:
#: тяжёлый рантайм (`anyio`, subprocess-адаптеры) и CLI.
FORBIDDEN_MODULES: Final[tuple[str, ...]] = (
    "anyio",
    "disputatio.cli",
    "disputatio.runtime",
)

#: Дандеры, которые интерпретатор кладёт в любой модуль сам, — они не часть
#: публичной поверхности и из сравнения с `__all__` исключаются.
STANDARD_DUNDERS: Final[frozenset[str]] = frozenset(
    {
        "__all__",
        "__builtins__",
        "__cached__",
        "__doc__",
        "__file__",
        "__loader__",
        "__name__",
        "__package__",
        "__path__",
        "__spec__",
    }
)

#: Проба в чистом процессе: что видно после голого `import disputatio`.
_PROBE: Final[str] = """
import json
import sys

import disputatio

print(
    json.dumps(
        {
            "modules": sorted(sys.modules),
            "attrs": sorted(vars(disputatio)),
            "version": disputatio.__version__,
        }
    )
)
"""


@lru_cache(maxsize=1)
def _clean_import_probe() -> dict[str, object]:
    """Результат `_PROBE` из отдельного процесса (общий для нескольких тестов)."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(result.stdout.decode())


def _probe_list(key: str) -> list[str]:
    """Список строк из пробы под ключом `key`."""
    value = _clean_import_probe()[key]
    assert isinstance(value, list)
    return value


def test_star_import_exports_exactly_all() -> None:
    """`from disputatio import *` даёт множество, точно равное `set(__all__)`."""
    exported = getattr(disputatio, "__all__", None)
    assert exported is not None, "`__all__` обязан быть определён явно"
    assert list(exported), "пустой `__all__` делает проверку вакуумной"

    namespace: dict[str, object] = {}
    exec("from disputatio import *", namespace)  # noqa: S102
    starred = set(namespace) - {"__builtins__"}

    assert starred == set(exported), (
        "звёздочный импорт разошёлся с `__all__`: "
        f"лишнее {sorted(starred - set(exported))}, "
        f"недостающее {sorted(set(exported) - starred)}"
    )


def test_version_matches_pyproject() -> None:
    """`__version__` — литерал, совпадающий с `project.version` в pyproject.toml."""
    pyproject = REPO_ROOT / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "version"
    ]

    assert isinstance(disputatio.__version__, str)
    assert disputatio.__version__ == declared, (
        f"версия пакета {disputatio.__version__!r} разошлась с "
        f"project.version {declared!r} в {pyproject}"
    )


def test_every_exported_name_is_importable() -> None:
    """Каждое имя из `__all__` реально доступно как атрибут пакета, без дублей."""
    exported = list(getattr(disputatio, "__all__", []))
    assert exported, "`__all__` обязан быть определён явно и непуст"
    assert all(isinstance(name, str) for name in exported)
    assert len(exported) == len(set(exported)), f"дубли в `__all__`: {exported}"
    assert exported == sorted(exported), f"`__all__` не отсортирован: {exported}"

    missing = [name for name in exported if not hasattr(disputatio, name)]
    assert missing == [], f"имена из `__all__` не импортируются: {missing}"


def test_version_is_exported() -> None:
    """`__version__` — часть публичного контракта, а не случайный атрибут."""
    assert "__version__" in list(getattr(disputatio, "__all__", []))


def test_clean_import_leaks_no_submodule_names() -> None:
    """В чистом процессе имена подмодулей не становятся атрибутами пакета."""
    attrs = _probe_list("attrs")
    leaked = [name for name in SUBMODULES if name in attrs]

    assert leaked == [], f"подмодули протащены наружу: {leaked}"


def test_clean_import_leaks_no_private_or_extra_names() -> None:
    """Приватные и не заявленные в `__all__` имена наружу не протаскиваются."""
    exported = set(getattr(disputatio, "__all__", []))
    attrs = _probe_list("attrs")

    private = [
        name
        for name in attrs
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__"))
    ]
    assert private == [], f"приватные имена в поверхности пакета: {private}"

    extra = [
        name for name in attrs if name not in STANDARD_DUNDERS and name not in exported
    ]
    assert extra == [], f"поверхность пакета шире `__all__`: {extra}"


def test_clean_import_does_not_pull_runtime_or_cli() -> None:
    """Голый импорт пакета не втягивает `anyio`, `disputatio.runtime`, `cli`."""
    modules = set(_probe_list("modules"))
    pulled = [name for name in FORBIDDEN_MODULES if name in modules]

    assert "disputatio" in modules, "проба обязана реально импортировать пакет"
    assert pulled == [], f"голый импорт пакета втянул лишнее: {pulled}"
