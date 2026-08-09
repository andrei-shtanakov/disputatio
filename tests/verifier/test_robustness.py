"""Тесты TASK-005: устойчивость к сбоям запуска ([REQ-011], [DESIGN-006]).

Сбой одного gate — несуществующий бинарь, неразбираемая или пустая
команда, неисполняемый файл — не имеет права выбросить исключение наружу:
проблемный gate получает `skip` с непустым `reason`, остальные gates
выполняются, отчёт достраивается.

Импорт `disputatio.verifier.runner` выполняется лениво внутри
`_import_runner`: на red-чекпоинте модуля ещё нет, и импорт на уровне
модуля сломал бы collection всего каталога. Хелпер пересобирает
`ImportError` в `AssertionError` — гейт принимает red только при падении
assertion'ом.
"""

import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from disputatio.contracts.verification import GateStatus

if TYPE_CHECKING:
    from disputatio.verifier import GateSpec

_MISSING_BINARY = "disputatio-no-such-binary-xyz"


def _import_runner() -> ModuleType:
    """Импортирует `disputatio.verifier.runner`; отсутствие — `AssertionError`."""
    try:
        from disputatio.verifier import runner
    except ImportError as exc:  # red-фаза: runner.py ещё не создан
        raise AssertionError("src/disputatio/verifier/runner.py ещё не создан") from exc
    return runner


def _python_cmd(code: str) -> str:
    """Собирает команду `<интерпретатор> -c <code>` с корректным quoting'ом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_missing_binary_becomes_skip_without_raising(
    tmp_path: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """Несуществующий бинарь → skip с непустым `reason`, без исключения."""
    runner = _import_runner()
    spec = make_gate_spec(name="typecheck", cmd=f"{_MISSING_BINARY} --version")

    result = runner.run_gate(spec, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason
    assert _MISSING_BINARY in result.reason


def test_unclosed_quote_command_becomes_skip(
    tmp_path: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """Незакрытая кавычка (`ValueError` разбора) → skip с непустым `reason`."""
    runner = _import_runner()
    spec = make_gate_spec(name="tests", cmd="pytest -k 'unclosed")

    result = runner.run_gate(spec, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason


@pytest.mark.parametrize("cmd", ["", "   "])
def test_empty_command_becomes_skip(
    cmd: str, tmp_path: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """Пустая (или пробельная) команда → skip, а не сбой на пустом argv."""
    runner = _import_runner()
    spec = make_gate_spec(name="tests", cmd=cmd)

    result = runner.run_gate(spec, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason


def test_non_executable_file_becomes_skip(
    tmp_path: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """Файл без бита исполнения (`PermissionError`) → skip с непустым `reason`."""
    runner = _import_runner()
    script = tmp_path / "gate.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)
    spec = make_gate_spec(name="custom", cmd=shlex.quote(str(script)))

    result = runner.run_gate(spec, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason


def test_broken_gate_does_not_stop_the_remaining_gates(
    tmp_path: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """Сбойный gate в середине списка не мешает выполнению остальных.

    Маркерный файл третьего gate — наблюдаемое доказательство того, что
    прогон дошёл до конца списка, а не оборвался на втором ([REQ-011]).
    """
    runner = _import_runner()
    marker = tmp_path / "third-gate-ran.marker"
    specs = [
        make_gate_spec(name="first", cmd=_python_cmd("print('first')")),
        make_gate_spec(name="broken", cmd=f"{_MISSING_BINARY} check"),
        make_gate_spec(
            name="third",
            cmd=_python_cmd(f"import pathlib; pathlib.Path({str(marker)!r}).touch()"),
        ),
    ]

    results = [runner.run_gate(spec, tmp_path) for spec in specs]

    assert [result.name for result in results] == ["first", "broken", "third"]
    assert [result.status for result in results] == [
        GateStatus.PASS,
        GateStatus.SKIP,
        GateStatus.PASS,
    ]
    assert marker.exists()
