"""Обход границы INV-11 через алиас `from`-импорта: [REQ-002], [DESIGN-002].

Дыра, найденная мутационной пробой к `tests/runtime/test_core_purity.py`:
там `from`-половина пинится только формой `from disputatio.adapters import X`,
где запрещённый корень стоит в самом `node.module`. Форма
`from disputatio import events` прячет корень в алиасе — вырезание ветки
разбора алиасов переживало locked-файл целиком. Тот файл байт-неизменяем
после red-чекпоинта, поэтому пробел закрывается отдельным модулем рядом.
"""

import shutil
from pathlib import Path

import disputatio.core as _core_package
from disputatio.runtime import scan_package_purity

_CORE_PACKAGE_NAME = "disputatio.core"


def _core_copy(tmp_path: Path) -> Path:
    """Временная копия дерева `core` — площадка для негативных кейсов."""
    destination = tmp_path / "core"
    core_file = _core_package.__file__
    assert core_file is not None, "у disputatio.core нет __file__"
    shutil.copytree(
        Path(core_file).parent,
        destination,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return destination


def test_forbidden_root_hidden_in_from_alias_is_reported(tmp_path: Path) -> None:
    """`from disputatio import events` — тот же запрещённый корень, вид `from`."""
    core = _core_copy(tmp_path)
    rogue = core / "rogue_alias.py"
    rogue.write_text("from disputatio import events\n", encoding="utf-8")

    violations = scan_package_purity(core, package_name=_CORE_PACKAGE_NAME)

    assert [(v.module, v.lineno, v.imported, v.kind) for v in violations] == [
        (rogue, 1, "disputatio.events", "from")
    ]


def test_relative_alias_resolving_to_forbidden_root_is_reported(
    tmp_path: Path,
) -> None:
    """`from .. import runtime` внутри `core` разрешается в `disputatio.runtime`."""
    core = _core_copy(tmp_path)
    rogue = core / "rogue_relative_alias.py"
    rogue.write_text("from .. import runtime\n", encoding="utf-8")

    violations = scan_package_purity(core, package_name=_CORE_PACKAGE_NAME)

    assert [(v.module, v.lineno, v.imported, v.kind) for v in violations] == [
        (rogue, 1, "disputatio.runtime", "from")
    ]


def test_neighbouring_alias_with_shared_prefix_is_not_a_violation(
    tmp_path: Path,
) -> None:
    """Граница имени держится и на алиасах: `disputatio.eventsX` — не нарушение."""
    core = _core_copy(tmp_path)
    (core / "neighbour_alias.py").write_text(
        "from disputatio import eventsX, contextual\n", encoding="utf-8"
    )

    assert scan_package_purity(core, package_name=_CORE_PACKAGE_NAME) == []
