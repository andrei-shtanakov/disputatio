"""Статистика изменений раунда ([DESIGN-008], [REQ-007]).

Единственная git-команда пакета, которая дерево читает, а не мутирует:
`git diff --numstat HEAD`. Запускается тем же безшелльным способом, что и
gates ([DESIGN-004]), хотя argv здесь фиксирован раннером, а не приходит
из конфигурации ([REQ-010]).

Untracked-файлы в `git diff HEAD` не входят по определению — отдельная
фильтрация им не нужна.
"""

import subprocess
from pathlib import Path

from disputatio.contracts.verification import DiffStats

_NUMSTAT_ARGV = ("git", "diff", "--numstat", "HEAD")


def collect_diff_stats(workdir: Path) -> DiffStats:
    """`git diff --numstat HEAD` → DiffStats; без HEAD → DiffStats(0, 0, 0).

    Ветка ненулевого кода возврата заведена ради репозитория без коммитов
    («bad revision 'HEAD'»): сравнивать не с чем, поэтому наружу уходят
    нули, а не исключение ([DESIGN-008]). Срабатывает она, однако, на любой
    сбой git: «not a git repository», bare-репозиторий, недоступный `.git`
    дают тот же код 128, а stderr гасится (в объединённом со stdout потоке
    диагностика внесла бы строки, неотличимые от numstat'а).

    TODO: сбой git тем самым неотличим от «изменений нет» — при разборе
    исхода в TASK-008 нужен способ отличить пустой diff от несостоявшегося
    запуска. Отсутствующий бинарь `git` уже уходит наружу исключением
    (`FileNotFoundError`), как и в `capture.py` ([DESIGN-004]).
    """
    with subprocess.Popen(
        _NUMSTAT_ARGV,
        shell=False,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as proc:
        stream = proc.stdout
        output = "" if stream is None else stream.read()
        exit_code = proc.wait()
    if exit_code != 0:
        return DiffStats(files=0, insertions=0, deletions=0)
    return _parse_numstat(output)


def _parse_numstat(output: str) -> DiffStats:
    """Складывает строки `<insertions>\\t<deletions>\\t<path>` в `DiffStats`.

    Каждая строка — один файл, поэтому `files` считается по строкам, а не
    по путям: переименование даёт `old => new` в третьем поле и всё равно
    остаётся одним файлом. Строки короче трёх полей игнорируются — своего
    файла у них нет.
    """
    files = insertions = deletions = 0
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        files += 1
        insertions += _count(fields[0])
        deletions += _count(fields[1])
    return DiffStats(files=files, insertions=insertions, deletions=deletions)


def _count(field: str) -> int:
    """Число строк из поля numstat; `-` бинарного файла → 0 ([DESIGN-008])."""
    return int(field) if field.isdigit() else 0
