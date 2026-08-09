"""Тесты TASK-004: `duration_s` — длительность выполнения gate ([REQ-006]).

Измерение живёт внутри `run_gate_command` ([DESIGN-007]) и обязано
пережить коррекцию системных часов: замер берётся `time.monotonic()`, а
не `time.time()`. Тест на устойчивость подменяет именно `time.time` —
монотонные часы остаются настоящими, поэтому реализация на `monotonic()`
подмены не замечает, а реализация на стенных часах даёт отрицательную
или нулевую длительность и падает.
"""

import shlex
import sys
import time
from pathlib import Path

import pytest

from disputatio.contracts.verification import GateStatus
from disputatio.verifier.capture import run_gate_command
from disputatio.verifier.config import GateSpec
from disputatio.verifier.runner import run_gate

# Команда спит заметно дольше типичного джиттера планировщика, чтобы
# нижняя граница замера отличала настоящее измерение от константы.
_SLEEP_S = 0.25
_MIN_MEASURED_S = 0.15


def _python_cmd(code: str) -> str:
    """Собирает команду `<интерпретатор> -c <code>` с корректным quoting'ом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _sleep_cmd() -> str:
    """Команда, живущая `_SLEEP_S` секунд."""
    return _python_cmd(f"import time; time.sleep({_SLEEP_S})")


def test_executed_command_reports_non_negative_duration(tmp_path: Path) -> None:
    """[REQ-006]: выполненная команда → `duration_s` — неотрицательное число."""
    outcome = run_gate_command(_python_cmd("pass"), tmp_path)

    assert isinstance(outcome.duration_s, float)
    assert outcome.duration_s >= 0.0


def test_duration_covers_the_lifetime_of_the_process(tmp_path: Path) -> None:
    """Замер покрывает жизнь процесса, а не является константой.

    Провокация против `duration_s=0.0`: команда заведомо спит `_SLEEP_S`,
    поэтому длительность обязана быть не меньше `_MIN_MEASURED_S`.
    """
    outcome = run_gate_command(_sleep_cmd(), tmp_path)

    assert outcome.duration_s is not None
    assert outcome.duration_s >= _MIN_MEASURED_S


def test_duration_survives_a_backwards_jump_of_the_system_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[DESIGN-007]: замер на `monotonic()`, а не на `time.time()`.

    `time.time` подменён убегающими назад показаниями — так выглядит
    коррекция системных часов, случившаяся посреди работы gate. Замер на
    стенных часах дал бы отрицательную длительность; монотонный замер
    подмены не видит и по-прежнему покрывает время сна процесса.
    """
    ticks = iter([1_000_000.0 - 60.0 * n for n in range(1000)])
    monkeypatch.setattr(time, "time", lambda: next(ticks))

    outcome = run_gate_command(_sleep_cmd(), tmp_path)

    assert outcome.duration_s is not None
    assert outcome.duration_s >= _MIN_MEASURED_S


def test_executed_gate_carries_duration_into_the_gate_result(tmp_path: Path) -> None:
    """[REQ-006]: `duration_s` доходит до `GateResult` выполненного gate."""
    spec = GateSpec(name="ok", cmd=_python_cmd("pass"))

    result = run_gate(spec, tmp_path)

    assert result.status is GateStatus.PASS
    assert isinstance(result.duration_s, float)
    assert result.duration_s >= 0.0


def test_skipped_gate_leaves_duration_unset(tmp_path: Path) -> None:
    """[REQ-006]: у skip-gate процесса не было — `duration_s` остаётся `None`."""
    spec = GateSpec(name="off", cmd=_python_cmd("pass"), enabled=False)

    result = run_gate(spec, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.duration_s is None
