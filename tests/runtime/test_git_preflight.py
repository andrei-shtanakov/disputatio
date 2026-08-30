"""Pre-flight git — три проверки перед стартом ([REQ-010], [DESIGN-010]).

[TASK-004]. Тест пинит не «функция вернулась», а свойства, без которых
pre-flight перестаёт защищать `changes.patch` от чужих правок:

* отказ приходит доменным исключением с человеческим текстом, а не
  `CalledProcessError` в лицо пользователю (NFR-003, [DESIGN-020]);
* при любом отказе `.disputatio/` не создаётся — bootstrap идёт строго
  ПОСЛЕ pre-flight, иначе неудачный старт оставляет мусор в репозитории;
* untracked-файлы старт НЕ блокируют: `.disputatio/` сама untracked, а
  требование «удалите черновики» сделало бы инструмент недружелюбным;
* репозиторий без коммитов отвергается — `git diff HEAD` и `git reset`
  не к чему привязать;
* каждая git-команда собрана argv-списком без `shell`, с герметичным
  окружением и явной идентичностью `-c user.name=… -c user.email=…`
  ([DESIGN §4.2]) — иначе сессия читает и правит конфиг пользователя;
* `GitCli` и тривиальный фейк одинаково проходят `isinstance` порта
  `GitOps` (ADR-005) — цикл видит порт, а не класс.

Символы, которых до реализации нет (`preflight`, `GitCli`,
`GitCommandError`, `EmptyRepository`), берутся через `_attr`, а не
`from ... import` на уровне модуля: такой импорт уронил бы коллекцию всего
файла, и red-чекпоинт оказался бы ошибкой сборки, а не падением
assertion'ом.
"""

import itertools
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from disputatio.runtime import (
    DirtyWorkingTree,
    DisputatioError,
    GitOps,
    NotAGitRepository,
)

from ._fakes import GitOpsFakeBase

_SESSION_DIR = ".disputatio"

# Внешние переменные, каждая из которых перебивает то, что pre-flight
# настраивает сам: расположение репозитория (команда отработает успешно, но
# в ЧУЖОМ репозитории), подпись коммита (`GIT_AUTHOR_*` сильнее `-c
# user.name`) и конфиг прямо из окружения (`GIT_CONFIG_COUNT` приоритетнее
# локального `.git/config` и не отключается ни `GIT_CONFIG_GLOBAL`, ни
# `GIT_CONFIG_NOSYSTEM`). Все они выставляются в тесте намеренно враждебно.
_HOSTILE_ENV = {
    "GIT_DIR": "/nonexistent/hostile/.git",
    "GIT_WORK_TREE": "/nonexistent/hostile",
    "GIT_INDEX_FILE": "/nonexistent/hostile/index",
    "GIT_OBJECT_DIRECTORY": "/nonexistent/hostile/objects",
    "GIT_AUTHOR_NAME": "hostile-author",
    "GIT_AUTHOR_EMAIL": "hostile@example.invalid",
    "GIT_COMMITTER_NAME": "hostile-committer",
    "GIT_COMMITTER_EMAIL": "hostile@example.invalid",
    "GIT_CONFIG_COUNT": "1",
}

_FAILURE_STDERR = "fatal: hostile index is unreadable"


def _attr(name: str) -> Any:
    """Публичный символ `disputatio.runtime`; отсутствие — AssertionError."""
    import disputatio.runtime as runtime_pkg

    assert hasattr(runtime_pkg, name), (
        f"disputatio.runtime не экспортирует {name!r}: pre-flight не реализован"
    )
    return getattr(runtime_pkg, name)


@dataclass
class FakeGit(GitOpsFakeBase):
    """`GitOps`-фейк: журналирует вызовы, ни одной git-команды не запускает."""

    calls: list[str] = field(default_factory=list)

    def diff_head(self) -> str:
        """Пустой дифф — валидный ответ (`analyze`-режим)."""
        self.calls.append("diff_head")
        return ""

    def commit_round(self, round_no: int) -> None:
        """Журналирует коммит принятого раунда."""
        self.calls.append(f"commit_round:{round_no}")

    def reset_hard(self, rev: str) -> None:
        """Журналирует сброс к целевому коммиту."""
        self.calls.append(f"reset_hard:{rev}")

    def clean(self) -> None:
        """Журналирует уборку untracked-файлов прерванной попытки."""
        self.calls.append("clean")


@dataclass
class _RunSpy:
    """Перехватывает `subprocess.run`: журналирует argv и kwargs.

    По умолчанию делегирует настоящему `run` — тест проверяет форму вызова,
    не подменяя поведение git'а. С `fail_on` заданная подкоманда отвечает
    ненулевым кодом: спровоцировать реальный сбой `git status` в исправном
    репозитории иначе нечем, а трансляция кода возврата в доменную ошибку
    пиниться обязана.
    """

    real: Any
    fail_on: str | None = None
    calls: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, cmd: Any, **kwargs: Any) -> Any:
        """Записывает вызов и отдаёт либо подставной сбой, либо реальный запуск."""
        self.calls.append((cmd, kwargs))
        if self.fail_on is not None and self.fail_on in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", _FAILURE_STDERR)
        return self.real(cmd, **kwargs)


def _identity_value(cmd: list[str], key: str) -> str:
    """Значение `-c <key>=…` из argv; пустая строка — идентичность не задана."""
    prefix = f"{key}="
    for flag, pair in itertools.pairwise(cmd):
        if flag == "-c" and pair.startswith(prefix):
            return pair[len(prefix) :]
    return ""


def _init_repo(path: Path) -> None:
    """`git init` без коммита — репозиторий есть, `HEAD` не разрешается."""
    subprocess.run(
        ["git", "init", "--quiet", "-b", "main"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_not_a_git_repository_is_rejected(tmp_path: Path, git_env: None) -> None:
    """Каталог вне git отвергается `NotAGitRepository`, без `.disputatio/`."""
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(NotAGitRepository):
        _attr("preflight")(plain)

    assert not (plain / _SESSION_DIR).exists(), (
        "pre-flight создал каталог сессии на отказе — bootstrap идёт ПОСЛЕ него"
    )


def test_dirty_tracked_tree_is_rejected(git_repo: Path) -> None:
    """Незакоммиченная правка tracked-файла блокирует старт ([REQ-010])."""
    (git_repo / "README.md").write_text("правка пользователя\n", encoding="utf-8")

    with pytest.raises(DisputatioError) as excinfo:
        _attr("preflight")(git_repo)

    exc = excinfo.value
    assert isinstance(exc, DirtyWorkingTree), (
        f"грязное дерево отвергнуто не своей ошибкой: {type(exc).__name__}"
    )
    assert not isinstance(exc, subprocess.CalledProcessError), (
        "наружу утёк CalledProcessError — пользователю нужен текст, не код возврата"
    )
    message = str(exc)
    assert "README.md" in message, f"диагностика не называет грязный файл: {message!r}"
    assert "CalledProcessError" not in message
    assert not (git_repo / _SESSION_DIR).exists(), (
        "pre-flight создал каталог сессии на отказе"
    )


def test_untracked_files_do_not_block_start(git_repo: Path) -> None:
    """Untracked-черновики старт не блокируют: `.disputatio/` сама untracked."""
    (git_repo / "draft.md").write_text("черновик пользователя\n", encoding="utf-8")
    (git_repo / "notes").mkdir()
    (git_repo / "notes" / "scratch.txt").write_text("заметки\n", encoding="utf-8")

    assert _attr("preflight")(git_repo) is None


def test_repository_without_commits_is_rejected(tmp_path: Path, git_env: None) -> None:
    """Нет `HEAD` — нечего диффать и не на что сбрасывать, старт отклонён."""
    empty = tmp_path / "empty"
    empty.mkdir()
    _init_repo(empty)

    with pytest.raises(DisputatioError) as excinfo:
        _attr("preflight")(empty)

    assert isinstance(excinfo.value, _attr("EmptyRepository")), (
        f"репозиторий без коммитов отвергнут не своей ошибкой: {excinfo.value!r}"
    )
    assert not (empty / _SESSION_DIR).exists()


def test_clean_tree_with_commit_passes(git_repo: Path) -> None:
    """Чистое дерево с коммитом проходит и каталог сессии не создаёт."""
    assert _attr("preflight")(git_repo) is None
    assert not (git_repo / _SESSION_DIR).exists(), (
        "pre-flight создал `.disputatio/` сам — это обязанность bootstrap'а"
    )


def test_gitcli_and_fake_satisfy_the_gitops_port(git_repo: Path) -> None:
    """`GitCli` и фейк взаимозаменяемы для цикла: оба — `GitOps` (ADR-005)."""
    assert isinstance(_attr("GitCli")(git_repo), GitOps)
    assert isinstance(FakeGit(), GitOps)


def test_git_invocations_are_hermetic_argv(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каждый вызов git — argv без shell, с герметичным env и идентичностью."""
    for var, value in _HOSTILE_ENV.items():
        monkeypatch.setenv(var, value)
    real_run = subprocess.run
    spy = _RunSpy(real=real_run)
    monkeypatch.setattr(subprocess, "run", spy)

    _attr("preflight")(git_repo)

    assert spy.calls, "pre-flight не выполнил ни одной git-команды"
    for cmd, kwargs in spy.calls:
        assert isinstance(cmd, list), f"команда собрана не argv-списком: {cmd!r}"
        assert cmd[0] == "git", f"argv начинается не с git: {cmd!r}"
        assert kwargs.get("shell", False) is False, f"команда ушла через shell: {cmd!r}"
        env = kwargs.get("env")
        assert env is not None, (
            f"окружение не задано — git наследует переменные шелла: {cmd!r}"
        )
        for var in _HOSTILE_ENV:
            assert var not in env, (
                f"унаследована враждебная переменная {var} в вызове {cmd!r}"
            )
        assert env.get("GIT_CONFIG_NOSYSTEM") == "1", (
            f"системный gitconfig не отключён в вызове {cmd!r}"
        )
        assert env.get("GIT_CONFIG_GLOBAL") == os.devnull, (
            f"глобальный gitconfig не отключён в вызове {cmd!r}"
        )
        assert _identity_value(cmd, "user.name"), (
            f"нет `-c user.name=…` в вызове {cmd!r}"
        )
        assert _identity_value(cmd, "user.email"), (
            f"нет `-c user.email=…` в вызове {cmd!r}"
        )


def test_nonzero_git_exit_raises_git_command_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ненулевой код git → `GitCommandError` с командой и stderr в тексте."""
    real_run = subprocess.run
    spy = _RunSpy(real=real_run, fail_on="status")
    monkeypatch.setattr(subprocess, "run", spy)

    with pytest.raises(DisputatioError) as excinfo:
        _attr("preflight")(git_repo)

    git_command_error = _attr("GitCommandError")
    assert issubclass(git_command_error, DisputatioError)
    assert isinstance(excinfo.value, git_command_error), (
        f"сбой git пришёл не своей ошибкой: {type(excinfo.value).__name__}"
    )
    message = str(excinfo.value)
    assert "status" in message, f"в диагностике нет упавшей команды: {message!r}"
    assert _FAILURE_STDERR in message, f"в диагностике нет stderr git'а: {message!r}"
