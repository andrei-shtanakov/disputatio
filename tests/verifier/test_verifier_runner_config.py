"""Review-pass TASK-008: конфигурация `VerifierRunner` ([DESIGN-003]).

Отдельный файл, а не дополнение к `test_verifier_runner.py`: тот залочен
red-чекпоинтом задачи и обязан остаться байт-в-байт прежним. Здесь
закрываются две дыры основного набора — обе про конструктор, а не про
`verify()`.
"""

import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from disputatio.verifier import VerifierRunner

if TYPE_CHECKING:
    from disputatio.verifier import GateSpec


def _python_cmd(code: str) -> str:
    """Собирает команду `<интерпретатор> -c <code>` с корректным quoting'ом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.mark.parametrize("tail_lines", [0, -1])
def test_non_positive_tail_lines_rejected_at_construction(
    tmp_git_repo: Path, tail_lines: int
) -> None:
    """Невалидный `tail_lines` — `ValueError` конструктора, не ложный `pass`.

    Без этой проверки лимит доехал бы до `capture.run_gate_command`, чей
    `ValueError` `run_gate` обязан гасить в `skip` ([REQ-011]): каждый gate
    стал бы «пропущенным» с причиной `invalid command`, а `overall` —
    зелёным ([REQ-008]). Ошибка конфигурации однонаправленно опасна именно
    в эту сторону, поэтому падает громко и до запуска процессов.
    """
    with pytest.raises(ValueError, match="tail_lines"):
        VerifierRunner([], tmp_git_repo, tail_lines=tail_lines)


def test_tail_lines_one_is_accepted(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """Граница: `tail_lines=1` валиден — хвост из одной последней строки.

    Парный к проверке выше: без него запрет мог бы быть шире заявленного
    (например, `tail_lines < 2`) и тест на 0/-1 всё равно был бы зелёным.
    """
    spec = make_gate_spec(name="noisy", cmd=_python_cmd("print('a'); print('b')"))

    report = VerifierRunner([spec], tmp_git_repo, tail_lines=1).verify(1)

    assert report.gates[0].tail == "b"


def test_caller_list_mutation_does_not_change_configured_gates(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """Список gates копируется: правка у вызывающего кода не меняет прогон.

    w-runtime владеет списком и вправе переиспользовать его между
    сессиями; раннер, хранящий ссылку, молча прогнал бы дописанный туда
    gate и вернул бы отчёт, не соответствующий своей конфигурации.
    """
    specs = [make_gate_spec(name="tests", cmd=_python_cmd("raise SystemExit(0)"))]
    runner = VerifierRunner(specs, tmp_git_repo)

    specs.append(make_gate_spec(name="sneaked", cmd=_python_cmd("raise SystemExit(1)")))
    report = runner.verify(1)

    assert [gate.name for gate in report.gates] == ["tests"]
