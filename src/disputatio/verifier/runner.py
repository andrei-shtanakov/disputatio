"""Отображение исхода запуска в `GateResult` ([DESIGN-006], [REQ-004], [REQ-011]).

Статус — детерминированная функция исхода: `rc == 0` → `pass`, `rc != 0` →
`fail` с фактическим кодом, несостоявшийся запуск → `skip` с непустым
`reason` и `exit_code is None`.

Все исключения запуска перехватываются здесь, внутри обработки ОДНОГО
gate: `run_gate` не выбрасывает наружу ничего — сбойный gate становится
строкой отчёта, остальные gates выполняются, отчёт достраивается
([REQ-011]).
"""

from pathlib import Path

from disputatio.contracts.verification import GateResult, GateStatus
from disputatio.verifier.capture import DEFAULT_TAIL_LINES, run_gate_command
from disputatio.verifier.config import GateSpec

REASON_DISABLED = "disabled in config"


def run_gate(
    spec: GateSpec, workdir: Path, *, tail_lines: int = DEFAULT_TAIL_LINES
) -> GateResult:
    """Выполняет один gate в `workdir` и отображает исход в `GateResult`.

    `enabled=False` — заявленный конфигурацией skip: процесс не
    запускается вовсе. Прочие skip-ветки — несостоявшийся запуск:
    несуществующий бинарь (`FileNotFoundError`), неразбираемая или пустая
    команда (`ValueError`), отказ ОС в запуске (`PermissionError` и другие
    `OSError`). Во всех трёх случаях `exit_code` и `duration_s` остаются
    `None` — измерять нечего.

    `tail_lines` — сквозной лимит хвоста ([DESIGN-005]), приходящий от
    `VerifierRunner` ([DESIGN-003]); ограничение самого хвоста живёт в
    `capture`, здесь параметр только передаётся дальше.
    """
    if not spec.enabled:
        return _skipped(spec, REASON_DISABLED)

    try:
        outcome = run_gate_command(spec.cmd, workdir, tail_lines=tail_lines)
    except FileNotFoundError as exc:
        return _skipped(spec, f"command not found: {exc.filename or spec.cmd}")
    except ValueError as exc:
        return _skipped(spec, f"invalid command: {exc}")
    except OSError as exc:  # PermissionError и прочие отказы запуска
        return _skipped(spec, f"cannot execute: {exc}")

    status = GateStatus.PASS if outcome.exit_code == 0 else GateStatus.FAIL
    return GateResult(
        name=spec.name,
        cmd=spec.cmd,
        status=status,
        exit_code=outcome.exit_code,
        duration_s=outcome.duration_s,
        tail=outcome.tail,
    )


def _skipped(spec: GateSpec, reason: str) -> GateResult:
    """Собирает skip-результат: `exit_code is None`, `reason` непустой."""
    return GateResult(
        name=spec.name,
        cmd=spec.cmd,
        status=GateStatus.SKIP,
        reason=reason,
    )
