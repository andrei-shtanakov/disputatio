"""Запуск команды одного gate ([DESIGN-004], [REQ-003], [REQ-010]).

Единственная точка запуска процессов в пакете. Команда разбирается
`shlex.split` и запускается без shell'а: между раннером и проверяемым
процессом нет промежуточной оболочки, поэтому `returncode` по построению
принадлежит именно проверяемой команде, а не пайпу или обвязке. Хвост
вывода читается из пайпа самим раннером — никогда через `| tail`/`| tee`,
чей «зелёный» код возврата мог бы замаскировать красный тест.

Запускаются только команды, пришедшие из конфигурации gates ([REQ-010]).
"""

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Исход одного запуска: код возврата, вывод, длительность, ошибка запуска.

    `exit_code is None` — процесс не запускался (skip-ветки [DESIGN-006]);
    `error` несёт причину несостоявшегося запуска, `duration_s` — время
    жизни процесса ([DESIGN-007]).

    Сам `run_gate_command` такие исходы пока не возвращает: несостоявшийся
    запуск он выпускает наружу исключением, а `duration_s` не заполняет
    ([DESIGN-007]). Значения со skip-семантикой конструирует вызывающий
    код, отображающий исход в `GateResult` ([DESIGN-006]).
    """

    exit_code: int | None = None
    tail: str = ""
    duration_s: float | None = None
    error: str | None = None


def run_gate_command(cmd: str, workdir: Path) -> RunOutcome:
    """Выполняет `cmd` в `workdir` и возвращает исход запуска.

    stdout и stderr объединяются в один поток — хвост сохраняет реальный
    порядок интерливинга; `errors="replace"` защищает захват от не-UTF-8
    вывода. Исключения запуска (`FileNotFoundError`, `ValueError` разбора
    и прочие) наружу не перехватываются — их отображение в skip-ветки
    остаётся зоной вызывающего кода ([DESIGN-006]).
    """
    argv = shlex.split(cmd)
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
        output = "" if stream is None else stream.read()
        exit_code = proc.wait()
    return RunOutcome(exit_code=exit_code, tail=output)
