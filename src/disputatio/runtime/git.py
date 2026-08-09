"""Порт git-операций и pre-flight ([DESIGN-010]…[DESIGN-013], ADR-005).

`GitOps` — пятый порт оркестратора, объявленный в `runtime`, а не в
замороженных `contracts`: git-дисциплина §3 принадлежит циклу, а не схемам
артефактов. Здесь же живёт единственная реализация порта `GitCli` и
`preflight` — три проверки, без которых `changes.patch` перестаёт быть
диффом автора.

Все команды идут по одному протоколу ([DESIGN §4.2]): `subprocess.run` без
`shell`, argv-списком, с герметичным окружением и явной идентичностью через
`-c user.name=… -c user.email=…`. Идентичность передаётся флагами, а не
`git config`: сессия не вправе править конфиг пользовательского
репозитория, а унаследованные `GIT_AUTHOR_*`/`GIT_CONFIG_COUNT` перебили бы
её молча — поэтому окружение не наследуется, а собирается.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from disputatio.runtime.errors import (
    DirtyWorkingTree,
    EmptyRepository,
    GitCommandError,
    NotAGitRepository,
)

GIT_USER_NAME: Final = "disputatio"
GIT_USER_EMAIL: Final = "disputatio@localhost"

_IDENTITY_ARGS: Final = (
    "-c",
    f"user.name={GIT_USER_NAME}",
    "-c",
    f"user.email={GIT_USER_EMAIL}",
)

# Переменные окружения, каждая из которых перебивает то, что вызов
# настраивает сам. Расположение репозитория: при абсолютном `GIT_DIR`
# команда отработает успешно, но в ЧУЖОМ репозитории — `cwd` его не
# перебивает. Подпись: `GIT_AUTHOR_NAME` сильнее `-c user.name`, и
# идентичность из `_IDENTITY_ARGS` оказалась бы декоративной.
# `GIT_CONFIG_COUNT` — конфиг прямо из окружения, по приоритету он выше
# локального `.git/config` и не отключается ни `GIT_CONFIG_GLOBAL`, ни
# `GIT_CONFIG_NOSYSTEM`; без счётчика git не читает пары `GIT_CONFIG_KEY_n`.
_DROPPED_ENV_VARS: Final = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
    "GIT_CONFIG_COUNT",
)


@runtime_checkable
class GitOps(Protocol):
    """Порт git-операций рабочего репозитория (SPEC-001 §3)."""

    def diff_head(self) -> str:
        """`git diff HEAD` — дифф рабочего дерева; пустая строка валидна."""
        ...

    def commit_round(self, round_no: int) -> None:
        """Коммитит принятый раунд сообщением `disputatio: round NNN`."""
        ...

    def reset_hard(self, rev: str) -> None:
        """`git reset --hard <rev>` — откат к коммиту прошлого раунда."""
        ...

    def clean(self) -> None:
        """Удаляет untracked-файлы прерванной попытки, сохраняя `.disputatio/`."""
        ...


def preflight(root: Path) -> None:
    """Три проверки перед стартом сессии; успех — молча ([REQ-010]).

    Порядок из [DESIGN-010]: `root` — репозиторий, дерево чисто по
    tracked-файлам, `HEAD` существует. Untracked-файлы старт **не**
    блокируют: `.disputatio/` сама untracked, а требование «удалите
    черновики» сделало бы инструмент недружелюбным.

    Функция ничего не создаёт: `bootstrap_session` вызывается строго после
    неё, поэтому отказ не оставляет `.disputatio/` в чужом репозитории.
    """
    if not root.is_dir():
        raise NotAGitRepository(f"{root} — не каталог: стартовать сессию негде")
    if _run(root, "rev-parse", "--git-dir").returncode != 0:
        raise NotAGitRepository(
            f"{root} — не git-репозиторий; disputatio коммитит каждый принятый "
            "раунд, поэтому рабочая директория обязана быть под git"
        )
    status = _checked(root, "status", "--porcelain", "--untracked-files=no")
    if status.strip():
        raise DirtyWorkingTree(
            f"рабочее дерево {root} содержит незакоммиченные изменения "
            "tracked-файлов — закоммитьте или спрячьте их (`git stash`), "
            "иначе они попадут в changes.patch как работа автора:\n"
            f"{status.rstrip()}"
        )
    if _run(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        raise EmptyRepository(
            f"в репозитории {root} нет ни одного коммита — `git diff HEAD` и "
            "`git reset` не к чему привязать; сделайте первый коммит"
        )


@dataclass(frozen=True, slots=True)
class GitCli:
    """`GitOps` поверх git CLI: единственная реализация порта (ADR-005).

    Остальные методы порта приходят своими задачами ([DESIGN-011]…
    [DESIGN-013]) — заглушки здесь нужны, чтобы `RuntimeDeps.git` уже
    удовлетворял `GitOps`, и при этом не отдают поведения, которое эти
    задачи обязаны доказать собственным red-чекпоинтом.
    """

    root: Path

    def diff_head(self) -> str:
        """TODO: [TASK-005] — `git diff HEAD` с intent-to-add ([DESIGN-013])."""
        raise NotImplementedError("GitCli.diff_head приходит с [DESIGN-013]")

    def commit_round(self, round_no: int) -> None:
        """TODO: [TASK-006] — коммит `disputatio: round NNN` ([DESIGN-011])."""
        raise NotImplementedError("GitCli.commit_round приходит с [DESIGN-011]")

    def reset_hard(self, rev: str) -> None:
        """TODO: [TASK-007] — `git reset --hard <rev>` ([DESIGN-012])."""
        raise NotImplementedError("GitCli.reset_hard приходит с [DESIGN-012]")

    def clean(self) -> None:
        """TODO: [TASK-007] — уборка untracked прерванной попытки ([DESIGN-012])."""
        raise NotImplementedError("GitCli.clean приходит с [DESIGN-012]")


def _checked(root: Path, *args: str) -> str:
    """stdout команды; ненулевой код — `GitCommandError` с командой и stderr."""
    completed = _run(root, *args)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GitCommandError(
            f"git {' '.join(args)} упал с кодом {completed.returncode}: {detail}"
        )
    return completed.stdout


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Запускает git в `root`, не проверяя код возврата ([DESIGN §4.2]).

    Код возврата остаётся вызывающему: для `rev-parse` ненулевой код — это
    ответ «нет» (не репозиторий, нет `HEAD`), а не сбой. Трансляцию сбоя в
    доменную ошибку делает `_checked` — кроме одного случая: отсутствие
    самого клиента кода возврата не даёт, `exec` роняет `FileNotFoundError`
    мимо любой проверки, поэтому он переводится здесь (NFR-003).
    """
    try:
        return subprocess.run(
            ["git", *_IDENTITY_ARGS, *args],
            cwd=root,
            env=_env(),
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise GitCommandError(
            "git не найден в PATH: disputatio ведёт историю сессии коммитами, "
            "поэтому без git-клиента сессия не стартует"
        ) from exc


def _env() -> dict[str, str]:
    """Окружение git-вызова: без унаследованного, с отключённым конфигом."""
    env = dict(os.environ)
    for var in _DROPPED_ENV_VARS:
        env.pop(var, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env
