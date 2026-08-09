"""Дыры приёмочных сканеров, найденные ревью TASK-008: [NFR-001].

`test_acceptance_w_context.py` байт-неизменяем после red-чекпоинта, поэтому
закрытые дыры пинятся здесь. Каждая из них делала соответствующее
утверждение приёмки вакуумным:

* чистота пакета проверялась списком запрещённых модулей — `uuid`,
  `tempfile`, `shutil`, `sys` в него не входили, и модуль с `uuid.uuid4()`
  на импорте проходил скан, который обещает «ни I/O, ни случайности»;
* послабления типизатора искались только в `[[sub-config]]` — корневой
  `[errors] X = false`, `project-excludes` и выпадение из
  `project-includes` отключают проверку пакета целиком, оставляя
  `relaxed_package_modules()` пустым;
* граница внутренних зависимостей сравнивалась префиксом без точки, так
  что `disputatio.contextual` считался «своим» пакетом.
"""

import tomllib
from pathlib import Path

import pytest

from . import acceptance_scan as scan

PACKAGE_MODULE = "src/disputatio/context/author.py"


@pytest.mark.parametrize(
    "module",
    [
        "uuid",
        "tempfile",
        "shutil",
        "glob",
        "sys",
        "socket",
        "sqlite3",
        "platform",
        "urllib.request",
        "http.client",
    ],
)
def test_import_of_an_impure_module_is_an_offence(module: str) -> None:
    """Источник I/O или случайности ловится независимо от списка имён.

    Проверка чистоты обязана быть fail-closed: `uuid` в чёрном списке
    отсутствовал, и ровно этой дырой в TASK-001 уже прорастал
    недетерминированный тег.
    """
    assert scan.find_purity_offences(f"import {module}\n") == [f"import {module}"]
    root = module.partition(".")[0]
    assert scan.find_purity_offences(f"from {module} import thing\n") == [
        f"import {module}"
    ], f"from-форма импорта {root} должна ловиться наравне с точечной"


def test_pure_stdlib_and_internal_imports_stay_clean() -> None:
    """Обратная сторона: то, чем пакет действительно пользуется, чисто."""
    source = (
        "from collections.abc import Iterable\n"
        "from typing import Final\n"
        "import re\n"
        "from enum import StrEnum\n"
        "from disputatio.context.tags import wrap_artifact_data\n"
        "from disputatio.contracts.review import Issue\n"
    )

    assert scan.find_purity_offences(source) == []


def test_root_level_error_relaxation_covers_every_module() -> None:
    """Корневой `[errors]` отключает класс ошибок для всего репозитория.

    Без учёта этой секции критерий «`pyrefly check` без ошибок»
    закрывается одной строкой в конфиге — ровно тем способом, который
    приёмка обещает не пропустить.
    """
    config = tomllib.loads(
        'project-includes = ["**/*.py"]\n[errors]\nbad-assignment = false\n'
    )

    assert scan.typecheck_relaxations(config, [PACKAGE_MODULE]) == {
        PACKAGE_MODULE: ["errors bad-assignment"]
    }


def test_project_excludes_hides_the_package_from_the_typechecker() -> None:
    """Исключённый файл не проверяется вовсе — это сильнее послабления."""
    config = tomllib.loads(
        'project-includes = ["**/*.py"]\n'
        'project-excludes = ["src/disputatio/context/**"]\n'
    )

    assert scan.typecheck_relaxations(config, [PACKAGE_MODULE]) == {
        PACKAGE_MODULE: ["project-excludes src/disputatio/context/**"]
    }


def test_module_outside_project_includes_is_never_checked() -> None:
    """Файл, не попавший в `project-includes`, тоже даёт «0 errors»."""
    config = tomllib.loads('project-includes = ["tests/**/*.py"]\n')

    assert scan.typecheck_relaxations(config, [PACKAGE_MODULE]) == {
        PACKAGE_MODULE: ["вне project-includes"]
    }


def test_sub_config_relaxation_is_still_reported() -> None:
    """Регрессия: точечное послабление по-прежнему видно."""
    config = tomllib.loads(
        'project-includes = ["**/*.py"]\n'
        "[[sub-config]]\n"
        'matches = "src/disputatio/context/*.py"\n'
        "[sub-config.errors]\n"
        "read-only = false\n"
    )

    assert scan.typecheck_relaxations(config, [PACKAGE_MODULE]) == {
        PACKAGE_MODULE: ["sub-config src/disputatio/context/*.py: read-only"]
    }


def test_switching_a_check_on_is_not_a_relaxation() -> None:
    """`true` — включение проверки; принимать его за послабление нельзя."""
    config = tomllib.loads(
        'project-includes = ["**/*.py"]\n'
        "[errors]\n"
        "bad-assignment = true\n"
        "[[sub-config]]\n"
        'matches = "src/**"\n'
        "[sub-config.errors]\n"
        "read-only = true\n"
    )

    assert scan.typecheck_relaxations(config, [PACKAGE_MODULE]) == {}


def test_real_config_leaves_the_package_strictly_checked() -> None:
    """Сцепка сканера с настоящим `pyrefly.toml`, а не только с синтетикой."""
    assert scan.relaxed_package_modules() == {}
    assert scan.pyrefly_config_path().is_file()


@pytest.mark.parametrize(
    "module", ["disputatio.contextual", "disputatio.contractsx", "disputatio.core"]
)
def test_dependency_boundary_ends_at_a_dot(module: str) -> None:
    """`disputatio.contextual` — чужой пакет, а не префикс «своего»."""
    assert scan.find_dependency_violations(f"import {module}\n") == [module]


def test_allowed_internal_packages_are_not_violations() -> None:
    """Обратная сторона границы: сам пакет и контракты остаются своими."""
    source = (
        "from disputatio.context.tags import wrap_artifact_data\n"
        "from disputatio.contracts.review import Issue\n"
        "import disputatio.contracts\n"
    )

    assert scan.find_dependency_violations(source) == []


def test_nested_test_module_is_visible_to_the_orphan_detector(tmp_path: Path) -> None:
    """Тест-модуль в подкаталоге не должен ускользать от детектора сирот.

    Плоский `glob` не заглядывал в подкаталоги: модуль, положенный на
    уровень ниже, не числился ни в матрице, ни среди сирот — и приёмка
    отчитывалась о полноте, которой нет.
    """
    (tmp_path / "test_flat.py").write_text("", encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "test_nested.py").write_text("", encoding="utf-8")

    assert scan.test_modules_on_disk(tmp_path) == {"test_flat.py", "sub/test_nested.py"}
