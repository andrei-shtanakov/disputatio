"""Фикстуры тестов Verifier: tmp git-репозиторий и фабрика `GateSpec`.

Импорт `disputatio.verifier` выполняется лениво внутри фабрики: conftest
собирается pytest'ом и на red-фазе задач, когда пакета ещё может не быть, —
import на уровне модуля сломал бы collection всего каталога. Статической
типизации достаточно `TYPE_CHECKING`-ветки.
"""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from disputatio.verifier import GateSpec

# Глобальный/системный gitconfig разработчика отключён: `commit.gpgsign`,
# `core.hooksPath`, `commit.template`, `includeIf` иначе сорвали бы коммит
# фикстуры по причинам, не связанным с тестом. Фикстура задаёт всё нужное
# сама (`user.email`, `user.name`) — конфиг репозитория остаётся источником.
_HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

# Унаследованные `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` перебивают `cwd`:
# при абсолютном `GIT_DIR` в окружении (обёртки git-хуков, экспорт в шелле
# разработчика) все команды фикстуры отрабатывают успешно, но `.git` в
# `tmp_path` не появляется — `config` переписывает `user.*` ВНЕШНЕГО репо, а
# `add`/`commit` кладут туда настоящий коммит. Отказ при этом не громкий:
# падает уже снимок в тесте, когда чужое дерево изменено. Тот же список
# чистит `mutation_probe._git_env` — фикстура обязана быть герметичной не
# только по конфигу, но и по расположению репозитория.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _git_env() -> dict[str, str]:
    """Окружение git-команд фикстуры: герметичный конфиг, без чужого репозитория."""
    env = dict(os.environ)
    for key in _GIT_LOCATION_VARS:
        env.pop(key, None)
    return {**env, **_HERMETIC_GIT_ENV}


def _git(workdir: Path, *args: str) -> str:
    """Запускает `git *args` в `workdir`; ненулевой код возврата — ошибка.

    Возвращает stdout: тесты, проверяющие разбор патча, обязаны работать на
    НАСТОЯЩЕМ выводе git'а, а не на его пересказе строковым литералом —
    именно пересказ и оставил бинарные формы вне поля зрения (A1).

    Окружение герметично (`_git_env`), а сбой пересобирается в
    `RuntimeError` с stderr: `CalledProcessError.__str__` печатает только
    код возврата, и причина падения фикстуры иначе не видна в отчёте.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git {' '.join(args)} упал с кодом {exc.returncode}: "
            f"{(exc.stderr or exc.stdout or '').strip()}"
        ) from exc
    return completed.stdout


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Git-репозиторий во временном каталоге: init, `.gitignore`, tracked-файл.

    `.gitignore` перечисляет два кэш-каталога: `__pycache__/` (побочный
    продукт python-гейтов) и `.pytest_cache/` — [REQ-009] прямо объявляет
    появление таких кэшей допустимым, и без записи в игнорируемый каталог
    инвариант mutation-freedom проверялся бы только на пустом множестве.

    `tracked.txt` коммитится ради негативного контроля того же теста: без
    отслеживаемого файла портить в дереве нечего, кроме самого
    `.gitignore`, а его мутация меняла бы правила игнорирования по ходу
    проверки.
    """
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "verifier@tests.local")
    _git(tmp_path, "config", "user.name", "verifier-tests")
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "tracked.txt")
    _git(tmp_path, "commit", "--quiet", "-m", "init")
    return tmp_path


@pytest.fixture
def git_run() -> Callable[..., str]:
    """`git *args` в указанном репозитории → stdout, тем же герметичным окружением.

    Нужна тестам, которым важна форма вывода самого git'а (патч с бинарным
    файлом, переименованием, сменой режима): выдуманная строка патча
    проверяет разбор того, что тест сам же и придумал.
    """
    return _git


@pytest.fixture
def make_gate_spec() -> "Callable[..., GateSpec]":
    """Фабрика `GateSpec` с дефолтными name/cmd для лаконичных тестов."""

    def factory(
        name: str = "tests",
        cmd: str = "uv run pytest -q",
        *,
        enabled: bool = True,
    ) -> "GateSpec":
        from disputatio.verifier import GateSpec

        return GateSpec(name=name, cmd=cmd, enabled=enabled)

    return factory
