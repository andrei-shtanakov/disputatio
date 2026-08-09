"""Тесты `disputatio.verifier`: `GateSpec` (TASK-001) и статусы (TASK-005).

Импорты `disputatio.verifier` выполняются внутри тестов: на момент
red-чекпоинта пакета ещё нет, и импорт на уровне модуля сломал бы
collection. Red-селекторы превращают ImportError в AssertionError — гейт
принимает red только при падении assertion'ом. `disputatio.contracts`
существует с первой волны, поэтому `GateStatus` импортируется обычным
модульным импортом.
"""

import dataclasses
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


def test_gate_spec_frozen_slots_defaults() -> None:
    """`GateSpec` конструируется; frozen, slots, дефолт `enabled=True`."""
    try:
        from disputatio.verifier.config import GateSpec
    except ImportError as exc:  # red-фаза: config.py ещё не создан
        raise AssertionError("src/disputatio/verifier/config.py ещё не создан") from exc

    spec = GateSpec(name="tests", cmd="uv run pytest -q")
    assert spec.name == "tests"
    assert spec.cmd == "uv run pytest -q"
    assert spec.enabled is True

    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "mutated"  # type: ignore[read-only]

    assert spec.__slots__ == ("name", "cmd", "enabled")
    assert not hasattr(spec, "__dict__")


def test_gate_spec_reexported_from_package() -> None:
    """`GateSpec` — публичный реэкспорт из `disputatio.verifier`."""
    try:
        import disputatio.verifier
    except ImportError as exc:  # red-фаза: пакета ещё нет
        raise AssertionError("пакет disputatio.verifier ещё не создан") from exc

    from disputatio.verifier.config import GateSpec

    assert disputatio.verifier.GateSpec is GateSpec
    assert "GateSpec" in disputatio.verifier.__all__


def test_zero_exit_code_maps_to_pass(
    tmp_path: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """`rc == 0` → `pass`, фактический `exit_code`, `reason is None`."""
    runner = _import_runner()
    spec = make_gate_spec(name="fast", cmd=_python_cmd("print('gate-ok')"))

    result = runner.run_gate(spec, tmp_path)

    assert result.status is GateStatus.PASS
    assert result.exit_code == 0
    assert result.reason is None
    assert result.name == "fast"
    assert result.cmd == spec.cmd
    assert "gate-ok" in result.tail


def test_nonzero_exit_code_maps_to_fail(
    tmp_path: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """`rc != 0` → `fail` с фактическим кодом; `reason` остаётся пустым."""
    runner = _import_runner()
    spec = make_gate_spec(name="tests", cmd=_python_cmd("import sys; sys.exit(5)"))

    result = runner.run_gate(spec, tmp_path)

    assert result.status is GateStatus.FAIL
    assert result.exit_code == 5
    assert result.reason is None


def test_disabled_gate_is_skipped_without_starting_a_process(
    tmp_path: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """`enabled=False` → skip с непустым `reason`; команда не запускается.

    Маркерный файл — наблюдаемое доказательство того, что процесс не
    стартовал: заявленный skip обязан быть решением по конфигурации, а не
    результатом запуска ([DESIGN-006]).
    """
    runner = _import_runner()
    marker = tmp_path / "disabled-gate-ran.marker"
    spec = make_gate_spec(
        name="lint",
        cmd=_python_cmd(f"import pathlib; pathlib.Path({str(marker)!r}).touch()"),
        enabled=False,
    )

    result = runner.run_gate(spec, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason
    assert not marker.exists()
