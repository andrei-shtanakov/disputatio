"""Review-fix TASK-004: `duration_s` у ПРОВАЛИВШЕГОСЯ gate ([REQ-006]).

REQ-006 требует неотрицательную длительность для выполненного gate в обеих
ветках — `pass` И `fail`, — но `test_duration.py` пинит только `pass`.
Из-за этого мутация `duration_s=... if status is GateStatus.PASS else None`
в `runner.run_gate` переживала весь suite: отчёт молча терял стоимость
самой интересной проверки — той, что упала.

Файл отдельный: `test_duration.py` байт-залочен red-чекпоинтом TASK-004.
"""

import shlex
import sys
from pathlib import Path

from disputatio.contracts.verification import GateStatus
from disputatio.verifier.capture import run_gate_command
from disputatio.verifier.config import GateSpec
from disputatio.verifier.runner import run_gate

_FAILING_CODE = "import sys; sys.exit(3)"


def _failing_cmd() -> str:
    """Команда, завершающаяся ненулевым кодом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(_FAILING_CODE)}"


def test_failing_command_reports_non_negative_duration(tmp_path: Path) -> None:
    """[REQ-006]: ненулевой код возврата не отменяет замер в `RunOutcome`."""
    outcome = run_gate_command(_failing_cmd(), tmp_path)

    assert outcome.exit_code == 3
    assert isinstance(outcome.duration_s, float)
    assert outcome.duration_s >= 0.0


def test_failing_gate_carries_duration_into_the_gate_result(tmp_path: Path) -> None:
    """[REQ-006]: `duration_s` доходит до `GateResult` упавшего gate."""
    spec = GateSpec(name="boom", cmd=_failing_cmd())

    result = run_gate(spec, tmp_path)

    assert result.status is GateStatus.FAIL
    assert result.exit_code == 3
    assert isinstance(result.duration_s, float)
    assert result.duration_s >= 0.0
