"""Статистика изменений раунда ([DESIGN-008], [REQ-007]).

Дерево читают, а не мутируют, три git-команды пакета: основная
`git diff --numstat HEAD` и два read-only зонда `git rev-parse`
(`--is-inside-work-tree`, `--verify --quiet HEAD`), которыми ветка отказа
отличает репозиторий без коммитов от сломанного запуска ([DESIGN-001]).
Все три запускаются тем же безшелльным способом, что и gates
([DESIGN-004]), хотя argv здесь фиксирован раннером, а не приходит из
конфигурации ([REQ-010]).

Untracked-файлы в `git diff HEAD` не входят по определению — отдельная
фильтрация им не нужна.
"""

import subprocess
from pathlib import Path
from typing import Final

from disputatio.contracts.verification import DiffStats

_NUMSTAT_ARGV = ("git", "diff", "--numstat", "HEAD")
_WORKTREE_PROBE_ARGV = ("git", "rev-parse", "--is-inside-work-tree")
_HEAD_PROBE_ARGV = ("git", "rev-parse", "--verify", "--quiet", "HEAD")

# Сколько первых строк stderr уходит в сообщение об отказе
# ([DESIGN-005], [REQ-003]): вне репозитория stderr — 130 строк, из которых
# 129 — usage-справка `git diff --no-index`. Берётся голова, а не хвост:
# различающая строка (`fatal:` / `warning:`) стоит первой.
_STDERR_HEAD_LINES: Final = 20


def collect_diff_stats(workdir: Path) -> DiffStats:
    """`git diff --numstat HEAD` → DiffStats; репо без HEAD → DiffStats(0, 0, 0).

    Нули отдаются ровно в одном исходе: рабочее дерево есть, а `HEAD` не
    разрешается — репозиторий без коммитов, где сравнивать попросту не с
    чем ([REQ-001]). Любой другой ненулевой код возврата уходит наружу
    `RuntimeError` с argv, `workdir`, фактическим кодом и текстом git:
    сбой git больше не маскируется под «изменений нет» ([REQ-002]).

    Классификатор — зонд состояния репозитория, а не таблица кодов
    возврата ([DESIGN-001]): код 128 неоднозначен (его дают и репозиторий
    без коммитов, и bare-репозиторий — с противоположными требуемыми
    исходами), а вне репозитория код вовсе 129 — usage-ошибка fallback'а
    `--no-index`. Сообщения git к тому же локализуемы, тогда как
    `rev-parse --is-inside-work-tree` печатает машинный литерал
    `true`/`false`. Зонды запускаются только на ветке отказа, поэтому
    успешный путь не порождает лишних процессов ([DESIGN-002]).

    stderr захватывается **отдельным** потоком, а не сливается в stdout:
    в объединённом потоке строки диагностики попали бы на вход
    `_parse_numstat`, где они неотличимы от numstat'а ([REQ-005]).

    Отсутствующий бинарь `git` уходит наружу исключением
    (`FileNotFoundError`) и здесь не перехватывается, как и в `capture.py`
    ([DESIGN-004], [DESIGN-006]).
    """
    result = _run_git(_NUMSTAT_ARGV, workdir)
    if result.returncode == 0:
        return _parse_numstat(result.stdout)
    if _is_repo_without_head(workdir):
        return DiffStats(files=0, insertions=0, deletions=0)
    raise RuntimeError(
        f"`{' '.join(_NUMSTAT_ARGV)}` в {workdir} упал с кодом "
        f"{result.returncode}: {_diagnostic(result)}"
    )


def _run_git(argv: tuple[str, ...], workdir: Path) -> subprocess.CompletedProcess[str]:
    """Безшелльный запуск git в `workdir` с раздельным захватом потоков.

    `subprocess.run(capture_output=True)` читает оба пайпа одновременно
    (внутри — тот же `communicate()`), поэтому взаимная блокировка на
    заполненном буфере невозможна по построению — а она реальна: 130 строк
    usage в stderr вне репозитория ([DESIGN-003], [REQ-005]).
    Окружение не переопределяется, так что зонды говорят о том же
    репозитории, что и упавшая основная команда ([DESIGN-001]).
    """
    return subprocess.run(
        argv,
        shell=False,
        cwd=workdir,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _is_repo_without_head(workdir: Path) -> bool:
    """Рабочее дерево есть, но `HEAD` не разрешается — единственный случай нулей.

    stdout первого зонда читается наравне с его кодом возврата: в
    bare-репозитории `--is-inside-work-tree` даёт rc 0 и печатает `false`,
    так что классификация по одному коду пропустила бы bare в нули
    ([DESIGN-001], [REQ-007]).

    Второй зонд судится по паре (код, вывод): нерождённый HEAD — это rc 1
    при пустом выводе, и недоступный ref git показывает так же. Любая
    диагностика от зонда означает невыясненное состояние, а не отсутствие
    HEAD, и уходит наружу отказом.
    """
    worktree = _run_git(_WORKTREE_PROBE_ARGV, workdir)
    if worktree.returncode != 0 or worktree.stdout.strip() != "true":
        return False
    head = _run_git(_HEAD_PROBE_ARGV, workdir)
    # Ненулевого кода мало: `--quiet` молчит именно про нерождённый HEAD, а
    # заговоривший зонд сообщает о чём-то ещё, и читать это как «HEAD нет»
    # значило бы повторить исходный дефект уровнем ниже — «инструмент не смог
    # ответить» снова стало бы фактом о дереве (ревью PR #37).
    if head.returncode == 0:
        return False
    return not (head.stderr or head.stdout).strip()


def _diagnostic(result: subprocess.CompletedProcess[str]) -> str:
    """Голова диагностики git: stderr, а при пустом stderr — stdout."""
    text = (result.stderr or result.stdout or "").strip()
    return "\n".join(text.splitlines()[:_STDERR_HEAD_LINES])


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
