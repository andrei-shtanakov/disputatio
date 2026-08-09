"""Поиск коммита раунда не зависит от имён файлов в дереве ([DESIGN-012]).

Review fix [TASK-007]. `base_rev` для раунда N > 1 ищет коммит предыдущего
раунда командой `git log … HEAD`, и `HEAD` там стоит без завершающего `--`.
Для git это неоднозначность: ревизия `HEAD` и путь `HEAD` пишутся одинаково,
и когда в рабочем дереве есть файл (или каталог) с таким именем, команда
падает `fatal: ambiguous argument 'HEAD'` — цель сброса не вычисляется, а
`GitCommandError` уносит весь PROPOSING второго и каждого следующего раунда.

Тесты задачи этого не видели: репозиторий фикстуры файла `HEAD` не содержит,
а форма имени зависит от пользовательского репозитория, а не от сессии.
`diff_head` и `commit_round` от той же ловушки закрыты своими `--`;
разделитель нужен и здесь.

`reset_hard` свой `--` носил с самого начала, но ни один тест его не пинил —
он снимается без единого падения. Здесь же пинится и он: цель сброса
приходит из истории пользователя и вправе совпасть с именем файла в дереве.
"""

import subprocess
from pathlib import Path

from disputatio.runtime import GitCli, base_rev
from disputatio.runtime.git import ROUND_COMMIT_TEMPLATE

_AMBIGUOUS_NAMES = ("HEAD", "master")


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


def test_file_named_head_does_not_break_the_round_lookup(git_repo: Path) -> None:
    """Файл `HEAD` в дереве не превращает поиск цели в отказ git."""
    round_1 = _commit_file(
        git_repo, "round_1.py", ROUND_COMMIT_TEMPLATE.format(round=1)
    )
    for name in _AMBIGUOUS_NAMES:
        (git_repo / name).write_text("не ревизия, а файл\n", encoding="utf-8")
    _git(git_repo, "add", "--all")
    _git(git_repo, "commit", "--quiet", "-m", "имена, совпадающие с ревизиями")

    target = base_rev(git_repo, 2, base_commit=round_1)

    assert target == round_1, (
        "цель сброса раунда 2 не найдена при файле с именем ревизии в дереве: "
        f"{target!r} вместо коммита раунда 001 {round_1!r}"
    )


def test_untracked_directory_named_head_does_not_break_the_lookup(
    git_repo: Path,
) -> None:
    """Тот же файл `HEAD` untracked: уборка ещё не прошла, цель уже нужна."""
    round_1 = _commit_file(
        git_repo, "round_1.py", ROUND_COMMIT_TEMPLATE.format(round=1)
    )
    (git_repo / "HEAD").mkdir()
    (git_repo / "HEAD" / "draft.py").write_text("черновик\n", encoding="utf-8")

    target = base_rev(git_repo, 2, base_commit=round_1)

    assert target == round_1, (
        "untracked-каталог с именем ревизии сорвал поиск цели: сброс перед "
        f"PROPOSING идёт ДО уборки, {target!r} вместо {round_1!r}"
    )


def test_reset_target_colliding_with_a_path_is_read_as_a_revision(
    git_repo: Path,
) -> None:
    """Цель сброса, совпавшая с именем файла в дереве, — всё ещё ревизия."""
    target_sha = _git(git_repo, "rev-parse", "HEAD").strip()
    _git(git_repo, "branch", "target")
    _commit_file(git_repo, "target", "файл, названный как ветка")
    (git_repo / "target").write_text("правка прерванной попытки\n", encoding="utf-8")

    GitCli(git_repo).reset_hard("target")

    assert _git(git_repo, "rev-parse", "HEAD").strip() == target_sha, (
        "сброс на ветку `target` не состоялся при одноимённом файле в дереве"
    )
    assert not (git_repo / "target").exists(), (
        "файл `target` пережил сброс: git разобрал цель как путь, а не как "
        "ревизию, и вместо сброса дерева сделал что-то другое"
    )
