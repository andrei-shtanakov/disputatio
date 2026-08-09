"""Тесты `diffstats.collect_diff_stats` (TASK-006, [DESIGN-008], [REQ-007]).

Импорт `disputatio.verifier.diffstats` выполняется внутри тестов: на
момент red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы
collection всего каталога. Red-селектор превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
`disputatio.contracts` существует с первой волны и импортируется обычным
модульным импортом.
"""

import os
import subprocess
from pathlib import Path
from types import ModuleType

from disputatio.contracts.verification import DiffStats

# Герметичное окружение git: глобальный/системный конфиг разработчика
# (`commit.gpgsign`, `core.hooksPath`, `includeIf`) иначе сорвал бы
# подготовку репозитория по причине, не связанной с тестом. Дублирует
# `conftest._HERMETIC_GIT_ENV` осознанно — фикстуры conftest дают
# репозиторий С коммитом, а тесту про отсутствующий HEAD нужен репозиторий
# БЕЗ него, то есть собственный `git init`.
_HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _import_diffstats() -> ModuleType:
    """Импортирует `disputatio.verifier.diffstats`; отсутствие — `AssertionError`."""
    try:
        from disputatio.verifier import diffstats
    except ImportError as exc:  # red-фаза: diffstats.py ещё не создан
        raise AssertionError(
            "src/disputatio/verifier/diffstats.py ещё не создан"
        ) from exc
    return diffstats


def _git(workdir: Path, *args: str) -> None:
    """Запускает `git *args` в `workdir`; ненулевой код возврата — ошибка.

    Сбой пересобирается в `RuntimeError` с stderr: `CalledProcessError`
    печатает только код возврата, и причина падения подготовки иначе не
    видна в отчёте.
    """
    try:
        subprocess.run(
            ["git", *args],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **_HERMETIC_GIT_ENV},
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git {' '.join(args)} упал с кодом {exc.returncode}: "
            f"{(exc.stderr or exc.stdout or '').strip()}"
        ) from exc


def test_uncommitted_changes_report_actual_counts(tmp_git_repo: Path) -> None:
    """Изменения против HEAD → фактические files/insertions/deletions.

    Правки подобраны так, чтобы счётчики не зависели от эвристик
    diff-алгоритма: `grown.txt` только дополняется, `shrunk.txt` только
    усекается. Untracked-файл — негативный контроль: `git diff HEAD` его
    не видит, и в статистику он попасть не должен ([DESIGN-008]).
    """
    diffstats = _import_diffstats()
    grown = tmp_git_repo / "grown.txt"
    shrunk = tmp_git_repo / "shrunk.txt"
    grown.write_text("alpha\n", encoding="utf-8")
    shrunk.write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(tmp_git_repo, "add", "grown.txt", "shrunk.txt")
    _git(tmp_git_repo, "commit", "--quiet", "-m", "baseline")

    grown.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    shrunk.write_text("one\n", encoding="utf-8")
    (tmp_git_repo / "untracked.txt").write_text("noise\n", encoding="utf-8")

    stats = diffstats.collect_diff_stats(tmp_git_repo)

    assert isinstance(stats, DiffStats)
    assert stats.files == 2
    assert stats.insertions == 2
    assert stats.deletions == 2


def test_clean_tree_reports_zeroes(tmp_git_repo: Path) -> None:
    """Дерево без изменений против HEAD → все три значения 0 ([REQ-007])."""
    diffstats = _import_diffstats()

    stats = diffstats.collect_diff_stats(tmp_git_repo)

    assert (stats.files, stats.insertions, stats.deletions) == (0, 0, 0)


def test_binary_file_counts_in_files_but_not_in_lines(tmp_git_repo: Path) -> None:
    r"""Бинарный файл (`numstat` даёт `-\t-\t<path>`) → +1 к `files`, 0 к строкам.

    Байты `range(256)` содержат NUL — git классифицирует файл как бинарный
    и не выдаёт по нему числа строк ([DESIGN-008]).
    """
    diffstats = _import_diffstats()
    (tmp_git_repo / "payload.bin").write_bytes(bytes(range(256)))
    _git(tmp_git_repo, "add", "payload.bin")

    stats = diffstats.collect_diff_stats(tmp_git_repo)

    assert stats.files == 1
    assert stats.insertions == 0
    assert stats.deletions == 0


def test_repo_without_head_reports_zeroes_without_raising(tmp_path: Path) -> None:
    """Репозиторий без HEAD → `DiffStats(0, 0, 0)`, без исключения наружу.

    Файл заведён в индекс: изменения в дереве есть, но точки сравнения
    нет — `git diff HEAD` упал бы «bad revision», и нули обязаны быть
    решением кода, а не следствием пустого дерева ([DESIGN-008]).
    """
    diffstats = _import_diffstats()
    _git(tmp_path, "init", "--quiet")
    (tmp_path / "first.txt").write_text("alpha\n", encoding="utf-8")
    _git(tmp_path, "add", "first.txt")

    stats = diffstats.collect_diff_stats(tmp_path)

    assert (stats.files, stats.insertions, stats.deletions) == (0, 0, 0)
