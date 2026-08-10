"""Pre-flight: отказы, которые обязаны остаться доменными ([REQ-010], NFR-003).

Ревью [TASK-004]. Файл рядом с байт-locked `test_git_preflight.py` и
закрывает три дыры, которые пережили полный suite мутационной пробой:

* `root` вообще не каталог (нет пути, путь — файл). Без явной проверки
  `subprocess.run(cwd=…)` роняет `FileNotFoundError`/`NotADirectoryError`
  — пользователь получает traceback стандартного исключения там, где
  [DESIGN-020] обещает одну человеческую строку. Проба: снятие проверки
  `root.is_dir()` не заметил ни один из 849 тестов.
* Герметичность окружения пиньется не всем списком `_DROPPED_ENV_VARS`:
  переменные подписи `GIT_*_DATE` и `GIT_CONFIG_SYSTEM` не проверял никто,
  и их вычистка тихо выпадала из кода. Дата подписи важна не меньше имени:
  унаследованный `GIT_COMMITTER_DATE` делает коммит раунда
  недетерминированным по времени.
* git отсутствует в `PATH`. Это не «ненулевой код», а `FileNotFoundError`
  из самого `exec` — единственный отказ git-слоя, который проходил мимо
  трансляции в `GitCommandError`.

Проверка идёт через публичный `disputatio.runtime`: приватные `_run`/`_env`
— деталь реализации, тест пинит наблюдаемое поведение `preflight`.
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from disputatio.runtime import (
    DisputatioError,
    GitCommandError,
    NotAGitRepository,
    preflight,
)

_SESSION_DIR = ".disputatio"

# Переменные подписи и конфига, которых нет во враждебном наборе
# байт-locked теста: `GIT_*_DATE` перебивают время коммита, а
# `GIT_CONFIG_SYSTEM` — путь к системному конфигу.
_HOSTILE_SIGNATURE_ENV = {
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_CONFIG_SYSTEM": "/nonexistent/hostile/system.gitconfig",
}


@dataclass
class _ArgvSpy:
    """Перехватывает `subprocess.run`, журналирует вызов и делегирует дальше."""

    real: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, cmd: Any, **kwargs: Any) -> Any:
        """Записывает kwargs вызова и отдаёт результат настоящего `run`."""
        self.calls.append(kwargs)
        return self.real(cmd, **kwargs)


def test_missing_root_is_rejected_with_domain_error(
    tmp_path: Path, git_env: None
) -> None:
    """Несуществующий путь — доменный отказ, а не `FileNotFoundError`."""
    missing = tmp_path / "no-such-directory"

    with pytest.raises(DisputatioError) as excinfo:
        preflight(missing)

    exc = excinfo.value
    assert isinstance(exc, NotAGitRepository), (
        f"несуществующий root отвергнут не своей ошибкой: {type(exc).__name__}"
    )
    assert not isinstance(exc, OSError), (
        "наружу утекло стандартное OSError — пользователю нужна строка, не errno"
    )
    assert str(missing) in str(exc), f"диагностика не называет путь: {str(exc)!r}"
    assert not missing.exists(), "pre-flight создал каталог, которого не было"


def test_file_as_root_is_rejected_with_domain_error(
    tmp_path: Path, git_env: None
) -> None:
    """Файл вместо каталога — тот же доменный отказ, без `NotADirectoryError`."""
    not_a_dir = tmp_path / "root.txt"
    not_a_dir.write_text("не каталог\n", encoding="utf-8")

    with pytest.raises(DisputatioError) as excinfo:
        preflight(not_a_dir)

    exc = excinfo.value
    assert isinstance(exc, NotAGitRepository), (
        f"файл вместо каталога отвергнут не своей ошибкой: {type(exc).__name__}"
    )
    assert not isinstance(exc, OSError), (
        "наружу утекло стандартное OSError вместо человеческой диагностики"
    )
    assert not (tmp_path / _SESSION_DIR).exists(), (
        "pre-flight создал каталог сессии на отказе"
    )


def test_signature_dates_and_system_config_are_not_inherited(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GIT_*_DATE` не наследуются, системный конфиг закрыт `os.devnull`."""
    for var, value in _HOSTILE_SIGNATURE_ENV.items():
        monkeypatch.setenv(var, value)
    spy = _ArgvSpy(real=subprocess.run)
    monkeypatch.setattr(subprocess, "run", spy)

    preflight(git_repo)

    assert spy.calls, "pre-flight не выполнил ни одной git-команды"
    for kwargs in spy.calls:
        env = kwargs.get("env")
        assert env is not None, "окружение не задано — git наследует переменные шелла"
        for var in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
            assert var not in env, (
                f"унаследована переменная подписи {var}: время коммита задаёт шелл"
            )
        assert env.get("GIT_CONFIG_SYSTEM") == os.devnull, (
            "системный конфиг не перенаправлен в os.devnull"
        )


def test_absent_git_binary_is_reported_as_domain_error(
    git_repo: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Нет git в `PATH` — `GitCommandError`, а не `FileNotFoundError` из `exec`."""
    empty_bin = tmp_path_factory.mktemp("empty-bin")
    monkeypatch.setenv("PATH", str(empty_bin))

    with pytest.raises(DisputatioError) as excinfo:
        preflight(git_repo)

    exc = excinfo.value
    assert isinstance(exc, GitCommandError), (
        f"отсутствие git пришло не своей ошибкой: {type(exc).__name__}"
    )
    assert not isinstance(exc, OSError), (
        "наружу утёк FileNotFoundError — пользователю нужна причина, не errno"
    )
    assert "git" in str(exc), f"диагностика не называет git: {str(exc)!r}"
