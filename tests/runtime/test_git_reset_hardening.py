"""Сброс перед PROPOSING: то, чего он не должен уметь ([DESIGN-012]).

[TASK-007], рядом с байт-locked `test_git_reset.py`. Тот пинит основной
контракт на репозиториях удобной формы; здесь — свойства, которые на такой
форме не проявляются, а стоят сессии целиком.

Уборка:

* `--exclude`. Когда сам `root` — untracked-каталог (сессия запущена в
  свежем подкаталоге чужого репозитория), под pathspec `:/` попадает он
  целиком, и исключение пути ВНУТРИ него уже ничего не решает: каталог
  сносится куском вместе с журналом. Единственный гейт здесь —
  gitignore-шаблон `--exclude`. `preflight` формы `root` не требует.
* отсутствие `-x`. С ним уборка раунда выносит и игнорируемое — venv,
  сборочный кэш, локальные артефакты пользователя. Работой автора они не
  являются: в `changes.patch` игнорируемое не попадает.

Вычисление цели:

* коммит раунда ищется среди предков `HEAD`, а не по всем ссылкам. Чужая
  ветка (в т. ч. оставшаяся от прошлой сессии в этом же репозитории) вправе
  нести своё «disputatio: round NNN», и `--all` сбросил бы текущую ветку на
  чужую работу — то есть стёр бы принятую.
* номер раунда проверяется ДО построения сообщения: иначе раунд 0 уходит в
  поиск несуществующего «disputatio: round -01» и получает отказ с текстом
  про оборванную историю вместо «раунды нумеруются с 1».
"""

import subprocess
from pathlib import Path

import pytest

from disputatio.runtime import BaseRevisionNotFound, GitCli, base_rev
from disputatio.runtime.git import ROUND_COMMIT_TEMPLATE

_SESSION_DIR = ".disputatio"

_SESSION_JSON = '{"state": "PROPOSING"}\n'
_ABORTED_LINE = "правка прерванной попытки\n"
_IGNORED_BODY = "сборочный артефакт пользователя\n"


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


def test_session_dir_survives_when_root_itself_is_untracked(git_repo: Path) -> None:
    """`root` без tracked-файлов: журнал сессии переживает уборку."""
    root = git_repo / "workdir"
    session = root / _SESSION_DIR
    session.mkdir(parents=True)
    (session / "session.json").write_text(_SESSION_JSON, encoding="utf-8")
    (root / "draft.py").write_text(_ABORTED_LINE, encoding="utf-8")

    GitCli(root).clean()

    assert not (root / "draft.py").exists(), (
        "проверка вакуумна: уборка не тронула даже черновик прерванной попытки"
    )
    assert (session / "session.json").read_text(encoding="utf-8") == _SESSION_JSON, (
        "журнал сессии снесён вместе с untracked-каталогом root: исключающий "
        "pathspec защищает путь внутри каталога, а удаляется каталог целиком"
    )


def test_ignored_files_of_the_user_survive_the_cleanup(git_repo: Path) -> None:
    """Игнорируемое — не работа автора: `-x` уборке раунда не передаётся."""
    (git_repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(git_repo, "add", ".gitignore")
    _git(git_repo, "commit", "--quiet", "-m", "user gitignore")
    build = git_repo / "build"
    build.mkdir()
    (build / "artifact.o").write_text(_IGNORED_BODY, encoding="utf-8")
    (git_repo / "draft.py").write_text(_ABORTED_LINE, encoding="utf-8")

    GitCli(git_repo).clean()

    assert not (git_repo / "draft.py").exists(), (
        "проверка вакуумна: уборка не тронула даже черновик прерванной попытки"
    )
    assert (build / "artifact.o").read_text(encoding="utf-8") == _IGNORED_BODY, (
        "уборка раунда вынесла игнорируемые файлы пользователя: в "
        "changes.patch они не попадают и работой автора не являются"
    )


def test_round_commit_of_another_branch_is_not_a_target(git_repo: Path) -> None:
    """Коммит раунда с чужой ветки целью сброса не становится."""
    base_commit = _git(git_repo, "rev-parse", "HEAD").strip()
    _commit_file(git_repo, "round_1.py", ROUND_COMMIT_TEMPLATE.format(round=1))
    _commit_file(git_repo, "round_2.py", ROUND_COMMIT_TEMPLATE.format(round=2))
    _git(git_repo, "checkout", "--quiet", "-b", "side")
    foreign = _commit_file(
        git_repo, "round_3.py", ROUND_COMMIT_TEMPLATE.format(round=3)
    )
    _git(git_repo, "checkout", "--quiet", "main")
    assert _git(git_repo, "rev-parse", "--verify", foreign).strip() == foreign, (
        "проверка вакуумна: коммит чужой ветки не существует, и поиск по всем "
        "ссылкам всё равно ничего бы не нашёл"
    )

    with pytest.raises(BaseRevisionNotFound):
        base_rev(git_repo, 4, base_commit=base_commit)


def test_round_below_one_is_rejected_before_a_subject_is_built(
    git_repo: Path,
) -> None:
    """Раунд 0 отвергается по номеру, а не отказом поиска «round -01»."""
    base_commit = _git(git_repo, "rev-parse", "HEAD").strip()

    with pytest.raises(BaseRevisionNotFound) as exc_info:
        base_rev(git_repo, 0, base_commit=base_commit)

    assert "disputatio: round" not in str(exc_info.value), (
        "из раунда 0 построено сообщение коммита: проверка номера стоит "
        f"после сборки шаблона, и отказ объясняет не ту причину:\n"
        f"{exc_info.value}"
    )
