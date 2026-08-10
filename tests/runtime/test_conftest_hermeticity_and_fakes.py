"""Дыры корневого `tests/conftest.py`, не закрытые TASK-002.

Файл байт-locked-теста `test_conftest_fixtures.py` менять нельзя, поэтому
пропущенное поведение пинится рядом. Четыре мутанта пережили полный suite:
снятое журналирование `session_ref`, потерянный проброс `session_ref` в
`AgentTurn`, тихий `dict.get` вместо громкого отказа `FakeVerifier` и
выброшенный `-b main`. Пятая дыра — не покрытие, а сама герметичность:
`git_env` снимал только переменные РАСПОЛОЖЕНИЯ репозитория, а подпись
коммита (`GIT_AUTHOR_*`/`GIT_COMMITTER_*`) и конфиг из окружения
(`GIT_CONFIG_COUNT`) перебивают локальный `user.*` и остаются в силе.
"""

import os
import subprocess
from functools import partial
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

from disputatio.contracts import AgentTurn, DiffStats, OverallStatus, VerificationReport

_ROOT_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"

# Окружение читающих git-команд самого теста: оно не должно зависеть от
# того, что проверяемая фикстура снимает (или не снимает) в своём.
_STRIPPED_VARS = (
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


def _conftest() -> ModuleType:
    """Корневой `tests/conftest.py`, загруженный по пути."""
    spec = importlib_util.spec_from_file_location(
        "disputatio_root_conftest_fakes", _ROOT_CONFTEST
    )
    assert spec is not None and spec.loader is not None, (
        f"{_ROOT_CONFTEST} не загружается как модуль"
    )
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake(name: str, **kwargs: Any) -> Any:
    """Экземпляр фейка `name` из корневого conftest'а."""
    factory = getattr(_conftest(), name, None)
    assert factory is not None, f"tests/conftest.py не объявляет {name}"
    return factory(**kwargs)


def _git(workdir: Path, *args: str) -> str:
    """Читающая git-команда в собственном герметичном окружении теста."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_VARS}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    result = subprocess.run(
        ["git", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} → {result.returncode}: {(result.stderr or '').strip()}"
    )
    return result.stdout.strip()


def _report(round_no: int, overall: OverallStatus) -> VerificationReport:
    """Минимальный `VerificationReport` для настройки `FakeVerifier`."""
    return VerificationReport(
        round=round_no,
        overall=overall,
        diff_stats=DiffStats(files=0, insertions=0, deletions=0),
    )


def test_external_commit_identity_does_not_sign_the_fixture_commit(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GIT_AUTHOR_*`/`GIT_COMMITTER_*` из окружения не подписывают коммит.

    Переменные подписи приоритетнее `user.*` из `.git/config`: без их
    снятия автором и коммиттером стартового коммита становится внешний
    разработчик, и набор начинает зависеть от шелла. Фикстура берётся
    через `getfixturevalue` — как параметр она отработала бы ДО того, как
    тест выставит переменные.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "external-author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@outside.local")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "external-committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@outside.local")

    repo: Path = request.getfixturevalue("git_repo")

    name = _git(repo, "config", "--local", "--get", "user.name")
    email = _git(repo, "config", "--local", "--get", "user.email")
    signature = _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>")
    assert signature == f"{name} <{email}>|{name} <{email}>"


def test_config_injected_through_the_environment_is_neutralized(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GIT_CONFIG_COUNT` не переопределяет локальный `user.name` фикстуры.

    Конфиг из окружения приоритетнее `.git/config` и не отключается ни
    `GIT_CONFIG_GLOBAL=os.devnull`, ни `GIT_CONFIG_NOSYSTEM`.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "injected-name")

    repo: Path = request.getfixturevalue("git_repo")

    name = _git(repo, "config", "--local", "--get", "user.name")
    assert _git(repo, "log", "-1", "--format=%an") == name
    assert "GIT_CONFIG_COUNT" not in os.environ


def test_git_repo_branch_is_main(git_repo: Path) -> None:
    """Имя ветки зафиксировано фикстурой, а не `init.defaultBranch`."""
    assert _git(git_repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_fake_adapter_journals_and_passes_through_the_session_ref() -> None:
    """`session_ref` попадает и в журнал вызовов, и в возвращённый `AgentTurn`."""
    adapter = _fake("FakeAdapter", replies=["первый", "второй"])

    first = anyio.run(partial(adapter.run, "промпт", session_ref="сессия-1"))
    second = anyio.run(partial(adapter.run, "ещё промпт"))

    assert isinstance(first, AgentTurn)
    assert first.session_ref == "сессия-1", "session_ref не проброшен в AgentTurn"
    assert second.session_ref is None
    assert adapter.session_refs == ["сессия-1", None]


def test_fake_verifier_refuses_an_unconfigured_round() -> None:
    """Незаданный раунд — громкий `AssertionError`, а не тихий `None`."""
    verifier = _fake("FakeVerifier", reports={1: _report(1, OverallStatus.PASS)})

    with pytest.raises(AssertionError):
        verifier.verify(2)
