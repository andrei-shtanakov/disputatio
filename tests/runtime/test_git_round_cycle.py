"""Сцепка `diff_head` и `commit_round` через раунды ([REQ-011], [REQ-013]).

Review-fix к [TASK-006]. Обе операции живут в одном репозитории и обязаны
переживать друг друга: `commit_round` прячет `.disputatio/` правилом в
`.git/info/exclude`, и это правило остаётся в репозитории навсегда — со
второго раунда `diff_head` работает уже в репозитории с игнорируемым
каталогом сессии.

Собственные тесты задач этой сцепки не видят: [TASK-005] снимает дифф в
репозитории без правила игнора, [TASK-006] коммитит, ни разу не позвав
`diff_head`. Поэтому здесь — цикл целиком: дифф раунда N, коммит раунда N,
дифф раунда N+1.
"""

import subprocess
from pathlib import Path

from disputatio.events.paths import SESSION_DIR_NAME
from disputatio.runtime import GitCli

_CREATED_FIRST = "pkg/first.py"
_CREATED_SECOND = "pkg/second.py"


def _git(workdir: Path, *args: str) -> str:
    """Вспомогательная git-команда теста; ненулевой код — `CalledProcessError`."""
    completed = subprocess.run(
        ["git", *args],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _write_session_artifacts(root: Path) -> None:
    """Журнал оркестратора, существующий всё время сессии."""
    session = root / SESSION_DIR_NAME
    session.mkdir(parents=True, exist_ok=True)
    (session / "session.json").write_text('{"state": "PROPOSING"}\n', encoding="utf-8")


def _write_created(root: Path, relative: str) -> None:
    """Новый модуль автора: его дифф виден только через intent-to-add."""
    created = root / relative
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text(f'VALUE = "{relative}"\n', encoding="utf-8")


def test_diff_head_survives_the_commit_of_the_previous_round(
    git_repo: Path,
) -> None:
    """Со второго раунда `diff_head` работает в репозитории с игнором сессии."""
    git = GitCli(git_repo)
    _write_session_artifacts(git_repo)
    _write_created(git_repo, _CREATED_FIRST)
    assert _CREATED_FIRST in git.diff_head(), (
        "проверка вакуумна: дифф первого раунда не содержит работы автора"
    )
    git.commit_round(1)

    _write_created(git_repo, _CREATED_SECOND)
    second = git.diff_head()

    assert _CREATED_SECOND in second, (
        "дифф второго раунда не содержит созданный автором файл — работа "
        f"раунда исчезла бы из ревью:\n{second!r}"
    )
    assert _CREATED_FIRST not in second, (
        "дифф второго раунда повторяет работу первого — коммит раунда не "
        f"стал базой сравнения:\n{second!r}"
    )


def test_diff_head_keeps_session_dir_untracked_after_a_round_commit(
    git_repo: Path,
) -> None:
    """`.disputatio/` остаётся untracked и после того, как игнор уже записан."""
    git = GitCli(git_repo)
    _write_session_artifacts(git_repo)
    _write_created(git_repo, _CREATED_FIRST)
    git.diff_head()
    git.commit_round(1)

    _write_created(git_repo, _CREATED_SECOND)
    git.diff_head()

    status = _git(git_repo, "status", "--porcelain")
    staged = [line for line in status.splitlines() if not line.startswith("??")]
    assert not any(SESSION_DIR_NAME in line for line in staged), (
        f"каталог сессии попал в индекс на втором раунде:\n{status}"
    )
    assert SESSION_DIR_NAME not in _git(git_repo, "ls-files"), (
        "артефакты сессии оказались под версионным контролем после цикла "
        f"раунда:\n{_git(git_repo, 'ls-files')}"
    )
