"""Запуск команды одного gate ([DESIGN-004], [REQ-003], [REQ-010]).

Единственная точка запуска процессов в пакете. Команда разбирается
`shlex.split` и запускается без shell'а: между раннером и проверяемым
процессом нет промежуточной оболочки, поэтому `returncode` по построению
принадлежит именно проверяемой команде, а не пайпу или обвязке. Хвост
вывода читается из пайпа самим раннером — никогда через `| tail`/`| tee`,
чей «зелёный» код возврата мог бы замаскировать красный тест.

Хвост ограничен построчно ([DESIGN-005], [REQ-005]): вывод читается по
строке за раз в кольцевой буфер длиной `tail_lines`, поэтому полный вывод
«болтливого» gate не удерживается в памяти целиком (NFR-004).

Запускаются только команды, пришедшие из конфигурации gates ([REQ-010]).
"""

import shlex
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Дефолтный лимит хвоста в строках ([DESIGN-005]): достаточно, чтобы
# ревьюер увидел суть падения gate, и мало, чтобы артефакт раунда не рос
# вместе с болтливостью команды ([REQ-005]).
DEFAULT_TAIL_LINES: Final = 200


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Исход одного запуска: код возврата, вывод, длительность, ошибка запуска.

    `exit_code is None` — процесс не запускался (skip-ветки [DESIGN-006]);
    `error` несёт причину несостоявшегося запуска, `duration_s` — время
    жизни процесса ([DESIGN-007]).

    Состоявшийся запуск `run_gate_command` всегда возвращает с
    заполненным `duration_s`; `None` остаётся только у исходов со
    skip-семантикой, которые конструирует вызывающий код, отображающий
    исход в `GateResult` ([DESIGN-006], [REQ-006]). Несостоявшийся запуск
    `run_gate_command` по-прежнему выпускает наружу исключением, а не
    исходом с `error`.
    """

    exit_code: int | None = None
    tail: str = ""
    duration_s: float | None = None
    error: str | None = None


def run_gate_command(
    cmd: str, workdir: Path, *, tail_lines: int = DEFAULT_TAIL_LINES
) -> RunOutcome:
    """Выполняет `cmd` в `workdir` и возвращает исход запуска.

    `tail` — не более `tail_lines` последних строк объединённого
    stdout+stderr, склеенных через `\\n`; вывод короче лимита попадает в
    хвост целиком, отсутствие вывода даёт `""` ([REQ-005]). Завершающий
    перевод строки самой команды в хвост не переносится, но пустые
    последние строки сохраняются как пустые элементы, поэтому вывод
    `"a\\n\\n\\n"` даёт хвост `"a\\n\\n"`.

    Строки читаются по одной в `deque(maxlen=tail_lines)`, поэтому дольше
    одной строки за раз полный вывод в памяти не живёт (NFR-004). Гарантия
    держится на строках: одна аномально длинная строка без переводов
    материализуется целиком — побайтового лимита [DESIGN-005] не вводит.

    `duration_s` — секунды по `time.monotonic()` от старта процесса до
    его завершения ([REQ-006], [DESIGN-007]). Монотонные часы, а не
    `time.time()`: коррекция системных часов посреди работы gate не должна
    делать длительность отрицательной или скачкообразной. Замер снимается
    здесь, а не в вызывающем коде, поэтому в него входит и чтение хвоста
    из пайпа — пока процесс жив, его вывод и есть часть его работы.

    stdout и stderr объединяются в один поток — хвост сохраняет реальный
    порядок интерливинга; `errors="replace"` защищает захват от не-UTF-8
    вывода. Исключения запуска (`FileNotFoundError`, `ValueError` разбора
    и прочие) наружу не перехватываются — их отображение в skip-ветки
    остаётся зоной вызывающего кода ([DESIGN-006]).

    Пустая (или пробельная) команда даёт пустой `argv` и отвергается
    `ValueError` здесь же: `Popen([])` вместо этого роняет `IndexError`,
    который вызывающему коду пришлось бы ловить отдельно от прочего
    невалидного `cmd` ([DESIGN-006] трактует оба случая одинаково).

    Оба аргумента проверяются ДО запуска процесса: `tail_lines < 1`
    отвергается тем же `ValueError`, иначе `deque(maxlen=-1)` уронил бы
    функцию уже после `Popen`, потеряв вывод и код возврата реального
    запуска, а `tail_lines == 0` тихо давал бы пустой хвост вопреки
    [REQ-005].
    """
    argv = shlex.split(cmd)
    if not argv:
        raise ValueError("empty command")
    if tail_lines < 1:
        raise ValueError(f"tail_lines must be >= 1, got {tail_lines}")
    started = time.monotonic()
    with subprocess.Popen(
        argv,
        shell=False,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as proc:
        stream = proc.stdout
        tail: deque[str] = deque(maxlen=tail_lines)
        if stream is not None:
            for line in stream:
                tail.append(line.rstrip("\n"))
        exit_code = proc.wait()
    return RunOutcome(
        exit_code=exit_code,
        tail="\n".join(tail),
        duration_s=time.monotonic() - started,
    )
