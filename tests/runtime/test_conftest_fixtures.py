"""Корневой `tests/conftest.py`: TASK-002, [REQ-023], [DESIGN-023], [ADR-008].

`commit_red` фиксирует весь `tests/`, поэтому `tests/conftest.py` пишется
ПОСЛЕ red — на red-чекпоинте его ещё нет. Отсутствующая фикстура даёт
pytest'у ошибку сбора, а не `AssertionError`, и гейт такой red не примет;
поэтому фикстуры запрашиваются не через сигнатуру теста, а через
`_fixture()` (`request.getfixturevalue` + перевод `FixtureLookupError` в
`AssertionError`), а классы фейков достаются `_conftest()` — загрузкой
модуля по пути с тем же переводом.

Герметичность проверяется с двух сторон: `git_env` обязан вычистить
унаследованные переменные расположения репозитория, а `git_repo` — не
увести коммит в чужой репозиторий, на который указывает абсолютный
`GIT_DIR` во внешнем окружении. Вторая половина не вытекает из первой:
`delenv` рядом с git-командой вместо `delenv` до неё прошёл бы проверку
переменных и всё равно закоммитил бы наружу.
"""

import inspect
import os
import subprocess
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

from disputatio.contracts import (
    AgentAdapter,
    AgentTurn,
    DiffStats,
    OverallStatus,
    VerificationReport,
    Verifier,
)

_ROOT_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"

# Переменные, перебивающие `cwd` git-команды: при абсолютном `GIT_DIR`
# фикстура успешно отработает, но репозиторий во временном каталоге не
# появится — все операции уйдут в чужое дерево.
_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


def _conftest() -> ModuleType:
    """Модуль `tests/conftest.py`; на red-фазе — `AssertionError`."""
    if not _ROOT_CONFTEST.exists():
        raise AssertionError(f"{_ROOT_CONFTEST} ещё не создан")
    spec = importlib_util.spec_from_file_location(
        "disputatio_root_conftest", _ROOT_CONFTEST
    )
    assert spec is not None and spec.loader is not None, (
        f"{_ROOT_CONFTEST} не загружается как модуль"
    )
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _attr(name: str) -> Any:
    """Публичный атрибут корневого conftest'а; отсутствие — `AssertionError`."""
    module = _conftest()
    value = getattr(module, name, None)
    assert value is not None, f"tests/conftest.py не объявляет {name}"
    return value


def _fixture(request: pytest.FixtureRequest, name: str) -> Any:
    """Значение фикстуры `name` — отсутствие как `AssertionError`, не как ошибка.

    Запрос через сигнатуру теста на red-фазе дал бы категорию `error`
    (`fixture ... not found`), и `tdd_gate red` отверг бы чекпоинт.
    """
    try:
        return request.getfixturevalue(name)
    except pytest.FixtureLookupError as exc:
        raise AssertionError(
            f"фикстура {name!r} не объявлена в tests/conftest.py"
        ) from exc


def _hermetic_env() -> dict[str, str]:
    """Окружение git-команд самого теста — не зависит от проверяемых фикстур."""
    env = {key: value for key, value in os.environ.items() if key not in _LOCATION_VARS}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(workdir: Path, *args: str) -> str:
    """Запускает `git *args` в `workdir` и возвращает stdout без хвостов."""
    result = subprocess.run(
        ["git", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        env=_hermetic_env(),
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} → {result.returncode}: {(result.stderr or '').strip()}"
    )
    return result.stdout.strip()


def _foreign_repo(root: Path) -> Path:
    """Чужой репозиторий-приманка: один коммит и опознаваемый `user.name`."""
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "foreign@tests.local")
    _git(root, "config", "user.name", "foreign-repo")
    (root / "keep.txt").write_text("foreign\n", encoding="utf-8")
    _git(root, "add", "keep.txt")
    _git(root, "commit", "--quiet", "-m", "foreign init")
    return root


def _report(round_no: int, overall: OverallStatus) -> VerificationReport:
    """Минимальный `VerificationReport` для настройки `FakeVerifier`."""
    return VerificationReport(
        round=round_no,
        overall=overall,
        diff_stats=DiffStats(files=0, insertions=0, deletions=0),
    )


def test_git_repo_lives_in_a_temporary_directory(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """`git_repo` — настоящий репозиторий внутри `tmp_path` теста."""
    repo = _fixture(request, "git_repo")

    assert isinstance(repo, Path), f"git_repo вернул {type(repo)!r}, а не Path"
    assert repo == tmp_path or tmp_path in repo.parents, (
        f"{repo} лежит вне временного каталога {tmp_path}"
    )
    assert (repo / ".git").is_dir(), f"в {repo} нет .git"
    assert Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() == repo.resolve()


def test_git_repo_identity_is_local_to_the_repository(
    request: pytest.FixtureRequest,
) -> None:
    """`user.name`/`user.email` записаны в `.git/config` и подписали коммит."""
    repo = _fixture(request, "git_repo")

    name = _git(repo, "config", "--local", "--get", "user.name")
    email = _git(repo, "config", "--local", "--get", "user.email")
    assert name, "user.name не задан локально"
    assert email, "user.email не задан локально"

    config_text = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert name in config_text, f"{name} не найден в .git/config"
    assert email in config_text, f"{email} не найден в .git/config"
    assert _git(repo, "log", "-1", "--format=%an <%ae>") == f"{name} <{email}>"


def test_git_repo_has_exactly_one_commit(request: pytest.FixtureRequest) -> None:
    """История фикстуры — ровно один коммит: база для `git reset` раундов."""
    repo = _fixture(request, "git_repo")

    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_git_env_drops_inherited_repository_location(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Все четыре переменные расположения репозитория удалены из окружения."""
    for var in _LOCATION_VARS:
        monkeypatch.setenv(var, str(tmp_path / f"inherited-{var}"))

    _fixture(request, "git_env")

    still_set = sorted(var for var in _LOCATION_VARS if var in os.environ)
    assert still_set == [], f"git_env не удалил {still_set}"


def test_git_env_neutralizes_user_and_system_git_config(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Глобальный и системный gitconfig отключены — `os.devnull` + NOSYSTEM."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/somewhere/.gitconfig")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/somewhere/gitconfig")
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    _fixture(request, "git_env")

    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull
    assert os.environ["GIT_CONFIG_SYSTEM"] == os.devnull
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"


def test_external_git_dir_does_not_capture_the_fixture_commit(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Абсолютный `GIT_DIR` в окружении не уводит коммит `git_repo` наружу."""
    foreign = _foreign_repo(tmp_path_factory.mktemp("foreign"))
    foreign_head = _git(foreign, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(foreign / ".git" / "objects"))

    repo = _fixture(request, "git_repo")

    assert (repo / ".git").is_dir(), f"репозиторий не создан в {repo}"
    assert _git(foreign, "rev-parse", "HEAD") == foreign_head, "коммит ушёл наружу"
    assert _git(foreign, "rev-list", "--count", "HEAD") == "1"
    assert _git(foreign, "config", "--local", "--get", "user.name") == "foreign-repo"
    assert _git(foreign, "status", "--porcelain") == "", "чужое дерево изменено"


def test_fake_adapter_satisfies_the_agent_adapter_port() -> None:
    """`FakeAdapter` проходит structural-check порта `AgentAdapter`."""
    fake_adapter = _attr("FakeAdapter")

    assert isinstance(fake_adapter(replies=["ok"]), AgentAdapter)


def test_fake_verifier_satisfies_the_verifier_port() -> None:
    """`FakeVerifier` проходит structural-check порта `Verifier`."""
    fake_verifier = _attr("FakeVerifier")

    assert isinstance(
        fake_verifier(reports={1: _report(1, OverallStatus.PASS)}), Verifier
    )


def test_fake_adapter_run_is_a_coroutine_function() -> None:
    """`FakeAdapter.run` — именно `async def`, а не sync-функция."""
    fake_adapter = _attr("FakeAdapter")

    assert inspect.iscoroutinefunction(fake_adapter.run)


def test_isinstance_alone_does_not_reject_a_sync_run() -> None:
    """Почему нужен `iscoroutinefunction`: `runtime_checkable` sync не ловит."""

    class SyncStub:
        def run(self, prompt: str, *, session_ref: str | None = None) -> AgentTurn:
            return AgentTurn(text=prompt)

    sync_stub: object = SyncStub()
    assert isinstance(sync_stub, AgentAdapter)
    assert not inspect.iscoroutinefunction(SyncStub.run)


def test_fake_adapter_journals_prompts_and_replays_the_reply_queue() -> None:
    """Промпты журналируются по порядку, ответы отдаются очередью."""
    adapter = _attr("FakeAdapter")(replies=["ответ автора", "ответ ревьюера"])

    first = anyio.run(adapter.run, "промпт автора")
    second = anyio.run(adapter.run, "промпт ревьюера")

    assert isinstance(first, AgentTurn)
    assert [first.text, second.text] == ["ответ автора", "ответ ревьюера"]
    assert adapter.prompts == ["промпт автора", "промпт ревьюера"]


def test_fake_adapter_refuses_a_call_beyond_the_queue() -> None:
    """Незапланированный вызов — громкий отказ, а не тихий пустой `AgentTurn`."""
    adapter = _attr("FakeAdapter")(replies=[])

    with pytest.raises(AssertionError):
        anyio.run(adapter.run, "лишний промпт")


def test_fake_verifier_returns_the_report_configured_for_the_round() -> None:
    """Отчёт выбирается по номеру раунда, обращения журналируются."""
    verifier = _attr("FakeVerifier")(
        reports={
            1: _report(1, OverallStatus.PASS),
            2: _report(2, OverallStatus.FAIL),
        }
    )

    assert verifier.verify(2).overall is OverallStatus.FAIL
    assert verifier.verify(1).overall is OverallStatus.PASS
    assert verifier.rounds == [2, 1]


def test_root_conftest_declares_no_autouse_fixtures() -> None:
    """[ADR-008]: ни одной autouse-фикстуры — наборы волн 0–1 не затронуты."""
    module = _conftest()

    markers: dict[str, Any] = {}
    for name, value in vars(module).items():
        marker = getattr(value, "_pytestfixturefunction", None)
        if marker is None:
            marker = getattr(value, "_fixture_function_marker", None)
        if marker is not None:
            markers[name] = marker

    assert {"git_env", "git_repo"} <= set(markers), (
        f"интроспекция не нашла фикстуры conftest'а: {sorted(markers)}"
    )
    autoused = sorted(name for name, marker in markers.items() if marker.autouse)
    assert autoused == [], f"ADR-008 запрещает autouse, найдены: {autoused}"
