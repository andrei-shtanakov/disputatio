"""Звёздочный импорт `disputatio` не связывает модули: follow-up TASK-023.

`tests/runtime/test_package_surface.py` заперт red-чекпоинтом и оставляет один
канал незакрытым: `__all__ = ["__version__", "runtime"]` проходит там весь файл
целиком. `hasattr(disputatio, "runtime")` внутри suite истинен из-за соседей по
`tests/runtime/`, множества звёздочного импорта и `set(__all__)` совпадают, а
голый `import disputatio` подмодуль по-прежнему не втягивает — проба смотрит
только на `vars(disputatio)`.

Между тем такой `__all__` и означает «имя подмодуля протащено наружу»: у
потребителя `from disputatio import *` свяжет модуль и втянет `anyio` вместе с
адаптерами и subprocess — ровно то, что запрещает [REQ-022], [DESIGN-022].
Проверки ниже пинят этот канал: ни одно имя из `__all__` не должно связываться
с модулем, и звёздочный импорт в чистом процессе не должен тянуть рантайм.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

#: Корень репозитория: tests/runtime/<файл> → parents[2].
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Модули, которых звёздочный импорт пакета втягивать не имеет права.
FORBIDDEN_MODULES: Final[tuple[str, ...]] = (
    "anyio",
    "disputatio.cli",
    "disputatio.runtime",
)

#: Проба в чистом процессе: что связывает и что втягивает `import *`.
_STAR_PROBE: Final[str] = """
import json
import sys
from types import ModuleType

namespace = {}
exec("from disputatio import *", namespace)

print(
    json.dumps(
        {
            "modules": sorted(sys.modules),
            "bound_modules": sorted(
                name
                for name, value in namespace.items()
                if name != "__builtins__" and isinstance(value, ModuleType)
            ),
        }
    )
)
"""


def _bound_modules(namespace: dict[str, object]) -> list[str]:
    """Имена namespace, связанные с модулями (служебный `__builtins__` — мимо)."""
    return sorted(
        name
        for name, value in namespace.items()
        if name != "__builtins__" and isinstance(value, ModuleType)
    )


def test_star_import_binds_no_modules() -> None:
    """`from disputatio import *` не связывает ни одного модуля."""
    namespace: dict[str, object] = {}
    exec("from disputatio import *", namespace)  # noqa: S102

    bound = _bound_modules(namespace)
    assert bound == [], f"звёздочный импорт связал модули: {bound}"


def test_star_import_in_clean_process_pulls_nothing_heavy() -> None:
    """В чистом процессе `import *` не тянет `anyio`, `runtime`, `cli`."""
    result = subprocess.run(
        [sys.executable, "-c", _STAR_PROBE],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    probe = json.loads(result.stdout.decode())

    assert "disputatio" in probe["modules"], "проба обязана импортировать пакет"
    assert probe["bound_modules"] == [], (
        f"звёздочный импорт связал модули: {probe['bound_modules']}"
    )

    pulled = [name for name in FORBIDDEN_MODULES if name in probe["modules"]]
    assert pulled == [], f"звёздочный импорт пакета втянул лишнее: {pulled}"
