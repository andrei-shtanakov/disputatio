"""Сброс перед PROPOSING под враждебным входом ([DESIGN-012]).

Review fix [TASK-007]. Три свойства, которых тесты задачи не касались, —
каждое отдаёт управление форме чужого репозитория или форме строки-цели:

* **локальный `.git/config`.** `_env` гасит системный и глобальный конфиг,
  но `.git/config` лежит внутри рабочей директории. `i18n.logOutputEncoding`
  перекодирует сообщение коммита ДО того, как по нему пройдёт `--grep`, и
  поиск коммита раунда не находит ничего. Отказ при этом обвиняет историю
  пользователя («оборвана либо переписана») — диагноз, по которому причину
  не восстановить. Ту же дыру для `changes.patch` уже закрыли `_DIFF_FLAGS`
  ([TASK-005]);
* **цель, начинающаяся с дефиса.** `git reset --hard --quiet --mixed`
  разбирает `--mixed` как опцию и делает mixed-сброс с кодом 0: метод
  обещает `--hard`, а правка прерванной попытки остаётся в дереве и утекает
  в следующий `changes.patch`;
* **`base_commit` вне истории `HEAD`.** Коммит раунда с чужой ветки целью
  не становится намеренно ([TASK-007], `test_git_reset_hardening.py`), а
  `base_commit` до этой правки становился — при том, что `git reset --hard`
  двигает ссылку ТЕКУЩЕЙ ветки и её коммиты после этого недостижимы.
"""

import subprocess
from pathlib import Path

import pytest

from disputatio.runtime import BaseRevisionNotFound, GitCli, GitCommandError, base_rev
from disputatio.runtime.git import ROUND_COMMIT_TEMPLATE

_ABORTED_LINE = "правка прерванной попытки\n"

# Кодировка, в которой ASCII-сообщение перестаёт быть ASCII-байтами: `--grep`
# сравнивает уже перекодированное, поэтому шаблон не совпадает ни с чем.
_HOSTILE_LOG_ENCODING = "UTF-16"


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


def _commit_file(root: Path, name: str, subject: str) -> str:
    """Коммитит новый файл сообщением `subject`; отдаёт SHA коммита."""
    (root / name).write_text(f"# {subject}\n", encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "--quiet", "-m", subject)
    return _git(root, "rev-parse", "HEAD").strip()


def _raw_grep_finds_nothing(root: Path, subject: str) -> bool:
    """Ищет ли коммит `subject` тот же `git log` БЕЗ `--encoding`."""
    completed = subprocess.run(
        [
            "git",
            "log",
            "--format=%H %s",
            "--fixed-strings",
            f"--grep={subject}",
            "HEAD",
            "--",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    return completed.stdout.strip() == ""


def test_local_log_encoding_does_not_hide_the_round_commit(git_repo: Path) -> None:
    """`i18n.logOutputEncoding` из чужого конфига не прячет цель сброса."""
    subject = ROUND_COMMIT_TEMPLATE.format(round=1)
    round_1 = _commit_file(git_repo, "round_1.py", subject)
    _git(git_repo, "config", "i18n.logOutputEncoding", _HOSTILE_LOG_ENCODING)
    assert _raw_grep_finds_nothing(git_repo, subject), (
        "проверка вакуумна: поиск находит коммит и без `--encoding`, значит "
        f"{_HOSTILE_LOG_ENCODING} в этой сборке git на вывод не влияет"
    )

    target = base_rev(git_repo, 2, base_commit=round_1)

    assert target == round_1, (
        "локальный конфиг репозитория спрятал коммит раунда от поиска: цель "
        f"сброса {target!r} вместо {round_1!r}"
    )


def test_option_like_target_is_not_a_silent_mixed_reset(git_repo: Path) -> None:
    """Цель `--mixed` — отказ, а не тихая подмена вида сброса."""
    head = _git(git_repo, "rev-parse", "HEAD").strip()
    (git_repo / "README.md").write_text(_ABORTED_LINE, encoding="utf-8")

    with pytest.raises(GitCommandError):
        GitCli(git_repo).reset_hard("--mixed")

    assert _git(git_repo, "rev-parse", "HEAD").strip() == head, (
        "HEAD сдвинулся на цели, которая ревизией не является"
    )


def test_base_commit_outside_head_history_is_rejected(git_repo: Path) -> None:
    """`base_commit` с чужой ветки — доменный отказ, а не цель сброса."""
    _git(git_repo, "checkout", "--quiet", "-b", "side")
    foreign = _commit_file(git_repo, "side.py", "работа чужой ветки")
    _git(git_repo, "checkout", "--quiet", "main")
    mine = _commit_file(git_repo, "mine.py", "работа текущей ветки")

    with pytest.raises(BaseRevisionNotFound):
        base_rev(git_repo, 1, base_commit=foreign)

    assert _git(git_repo, "rev-parse", "HEAD").strip() == mine, (
        "проверка вакуумна: текущая ветка не ушла вперёд, и сброс на чужой "
        "коммит ничего бы не стёр"
    )


def test_base_commit_in_head_history_is_still_accepted(git_repo: Path) -> None:
    """Проверка достижимости не ломает нормальный случай ([DESIGN-014])."""
    base_commit = _git(git_repo, "rev-parse", "HEAD").strip()
    _commit_file(git_repo, "round_1.py", ROUND_COMMIT_TEMPLATE.format(round=1))

    assert base_rev(git_repo, 1, base_commit=base_commit) == base_commit, (
        "предок HEAD отвергнут как цель раунда 1: проверка достижимости "
        "закрыла штатный сценарий вместо подмены истории"
    )
