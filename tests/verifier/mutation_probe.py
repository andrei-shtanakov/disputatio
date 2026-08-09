"""Снимок рабочего дерева и драйвер поверхности Verifier'а (TASK-009).

Не тест, а инструмент теста `test_mutation_freedom.py`: файл намеренно
назван без префикса `test_`, чтобы pytest не собирал его как модуль с
тестами. Живёт в `tests/`, а не в пакете — проверка [REQ-009] ничего не
добавляет к публичному API Verifier'а и не имеет права протащить в
`src/disputatio/verifier/**` ни одной новой git-команды ([REQ-010]).

`run_verifier_surface` — временный стенд-ин `VerifierRunner.verify()`
([DESIGN-003], TASK-008): выполняет ровно тот набор операций, из которых
`verify()` будет собран, — `run_gate` по каждому `GateSpec` и один
`collect_diff_stats`. TODO(TASK-008): заменить тело на вызов
`VerifierRunner(...).verify(round_no)`; тесты при этом не меняются.
"""

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from disputatio.contracts.verification import DiffStats, GateResult
from disputatio.verifier import GateSpec
from disputatio.verifier.diffstats import collect_diff_stats
from disputatio.verifier.runner import run_gate

# То же герметичное окружение, что и в `conftest._HERMETIC_GIT_ENV`:
# снимок обязан читать состояние репозитория фикстуры, а не глобальный
# конфиг разработчика (`core.excludesFile` подмешал бы посторонние правила
# игнорирования и мог бы скрыть настоящую мутацию).
_HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

# `GIT_DIR`/`GIT_WORK_TREE` из окружения перебивают `cwd` — под git-хуком
# (pre-commit гоняет suite) снимок молча читал бы ВНЕШНИЙ репозиторий, и
# чистое дерево дало бы «до == после» вообще без участия фикстуры.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

_PORCELAIN_ARGV = ("git", "status", "--porcelain")


def _git_env() -> dict[str, str]:
    """Окружение снимка: герметичный конфиг без унаследованных путей репозитория."""
    env = dict(os.environ)
    for key in _GIT_LOCATION_VARS:
        env.pop(key, None)
    return {**env, **_HERMETIC_GIT_ENV}


@dataclass(frozen=True, slots=True)
class SurfaceRun:
    """Исход прогона поверхности: результаты gates и статистика diff'а."""

    gates: list[GateResult]
    diff_stats: DiffStats


def run_verifier_surface(specs: Sequence[GateSpec], workdir: Path) -> SurfaceRun:
    """Прогоняет каждый `GateSpec` и собирает `DiffStats` — как сделает `verify()`.

    Порядок сохраняется: `diff_stats` снимается ПОСЛЕ всех gates, иначе
    мутация, устроенная последним gate'ом, не попала бы в статистику и
    негативный контроль теста оказался бы слепым к ней.
    """
    gates = [run_gate(spec, workdir) for spec in specs]
    return SurfaceRun(gates=gates, diff_stats=collect_diff_stats(workdir))


def porcelain(workdir: Path) -> str:
    """Снимок `git status --porcelain` для `workdir`.

    Ненулевой код возврата — ошибка снимка, а не «изменений нет»: молча
    вернув пустую строку, хелпер превратил бы сломанный git в вечно
    зелёный инвариант mutation-freedom. Игнорируемые файлы в вывод не
    входят по умолчанию (`--ignored` не передаётся) — именно поэтому
    запись в `.gitignore`'d каталог снимок не меняет ([REQ-009]).
    """
    result = subprocess.run(
        _PORCELAIN_ARGV,
        cwd=workdir,
        check=False,  # код возврата разбирается ниже, с диагностикой из stderr
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git status --porcelain в {workdir} упал с кодом "
            f"{result.returncode}: {(result.stderr or result.stdout or '').strip()}"
        )
    return result.stdout
