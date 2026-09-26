"""Архив RED-тестов закрытых волн: пины и правило «собран и прошёл».

Модуль без префикса `test_` — pytest его не собирает; его читают и тест-пин
(`test_frozen_red_archive.py`), и хук корневого `conftest.py`. Лежит вне scope
workstream'ов (не под `tests/test_*_red.py` и не под `tests/verifier/**`) и
перечислен в `harness_files`. Только stdlib: корневой conftest не тянет
продуктовый пакет на импорте.
"""

import fnmatch
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Final

#: sha256 байтов каждого архивного RED-теста (ревью #149, #151).
FROZEN_RED_ARCHIVE: Final[dict[str, str]] = {
    "tests/test_task_002_red.py": "c0b60e84bf1c31f8dd4a1e4ad4dd108701a6d41ee12391bfcb2a2ae024c02ce0",
    "tests/test_task_003_red.py": "085319123ce4ad5b8e6b495cf36e00cbe5c93f2db78691237e47f3ef296be51a",
    "tests/test_task_004_red.py": "c2a1bfe6666ea7ccc2c2e5b65f4ff9d39e48b658de6e146ddafc58bc287e0038",
    "tests/test_task_005_red.py": "7dcbc46cbb3c5a0bf593bfce8d3dcdd2e5f31dad2863915ea5c11a2292ef0431",
    "tests/test_ws57_task_001_red.py": "cc69ad508b1be6ee5f701f82402f38ddbbdba928148a2e257416cbea3743be43",
    "tests/test_ws57_task_002_red.py": "5a736a93c08d63eb04d5834a724409e2d9597e0366b1c9ebcc2690431534cc7a",
    "tests/test_ws57_task_005_red.py": "c67986fce8e19414e8519b82c7828d1a14f91badc73e210ee88f62a6fa045699",
    "tests/test_ws57_task_011_red.py": "931c74b9a239b49bcfb2e54442fa2c454d1d4c114b97e23779b39eef1aa707a8",
    "tests/test_ws57_task_014_red.py": "4cf25a6f19a0d649dbec8b7f0da5b5dedb7c44c2405b5b1db49a744182265cc5",
    "tests/test_ws57_task_015_red.py": "36bc6a991a7e8e27905bd58a2bcfc88a72d00a5471d1324c118cff2b85060bbc",
    "tests/verifier/test_task_001_red.py": "124da5d0d633cfec3f79bd55c05cb572fba16c213fd0513a430a8509dbd2041e",
    "tests/test_ws65_task_001_red.py": "1e6a927d004e0b957b7da437c307efa5f07e7dbe1c31b5ffe0d9e1edfd95726e",
}

#: Мощность пина — якорь, не выводимый из тех же списков (ревью #149): без
#: него опустошение пина вместе со строками `harness_files` давало бы сверку
#: пустых множеств, а `parametrize` по пустому набору — SKIPPED, не падение.
#: Меняется только вместе с архивом, отдельным PR.
FROZEN_RED_ARCHIVE_SIZE: Final = 12


def covered_archive(
    rootdir: Path,
    invocation_dir: Path,
    args: Sequence[str],
    *,
    ignore: Sequence[str] = (),
    ignore_glob: Sequence[str] = (),
) -> set[str]:
    """Архивные файлы, которые прогон с аргументами `args` собирает целиком.

    Файл покрыт, если он лежит под одним из путей запуска (или сам им
    является). Аргумент с `::` выбирает отдельные тесты, а не файл целиком,
    поэтому покрытия не даёт. Критерий — фактическая выборка, а не «аргументы
    не заданы»: `pytest -q tests` — такой же полный прогон, как `pytest -q`
    (ревью #152). `--ignore`/`--ignore-glob` вычитают из покрытия ровно
    игнорируемые пути, а не выключают правило (ревью #153). Фильтры
    `-k`/`-m`/`--lf`/`--deselect` проверяет вызывающий.
    """
    roots = [(invocation_dir / arg).resolve() for arg in args if "::" not in arg]
    ignored = [(invocation_dir / item).resolve() for item in ignore]
    # pytest абсолютизирует шаблоны `--ignore-glob` от каталога запуска
    # (`_pytest.main`: `absolutepath(x)`) до `fnmatch`; иначе относительный
    # шаблон не совпал бы ни с одним абсолютным путём (ревью #153).
    globs = [str(invocation_dir / pattern) for pattern in ignore_glob]
    covered: set[str] = set()
    for path in FROZEN_RED_ARCHIVE:
        target = (rootdir / path).resolve()
        if not any(_under(target, root) for root in roots):
            continue
        if any(_under(target, root) for root in ignored):
            continue
        if any(fnmatch.fnmatch(str(target), pattern) for pattern in globs):
            continue
        covered.add(path)
    return covered


def _under(target: Path, root: Path) -> bool:
    """`target` — это `root` или лежит под ним."""
    return target == root or root in target.parents


def archive_violations(
    outcomes: Mapping[str, Sequence[str]],
    paths: Collection[str] | None = None,
) -> list[str]:
    """Архивные файлы, чьи тесты в полном прогоне не собраны или не прошли.

    `outcomes` — исходы отчётов pytest по пути файла (`passed`, `skipped`,
    `failed`). Нарушение — файл без единого `passed` (не собран: например
    `collect_ignore` соседнего conftest'а) либо с любым иным исходом (skip,
    провал). Байты файла сторожит пин, исполнение — это правило (ревью #151).
    `paths` — файлы, которые прогон обязан был исполнить (`covered_archive`);
    по умолчанию весь архив.
    """
    violations: list[str] = []
    for path in sorted(FROZEN_RED_ARCHIVE if paths is None else paths):
        seen = outcomes.get(path, ())
        if "passed" not in seen:
            violations.append(f"{path}: не собран или не исполнен")
        elif any(outcome != "passed" for outcome in seen):
            violations.append(f"{path}: исходы {sorted(set(seen))}, ожидался passed")
    return violations
