"""Снимок входов `DECIDING` и критерий converged §5.1 (DESIGN-004/005).

`DecidingInputs`/`DecisionDraft` — frozen-датаклассы: значения, а не объекты
с поведением. `is_converged()` реализует критерий сходимости [REQ-007] и
анти-сикофантию [REQ-008] в одном месте — по DESIGN-005 защита раунда 1
живёт внутри критерия converged, не как отдельная ветка. `decide()`
(TASK-007) вызывает `is_converged()` первым шагом top-down порядка §5.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from disputatio.contracts import (
    BudgetUsed,
    Issue,
    Limits,
    Mode,
    Outcome,
    OverallStatus,
    Review,
    Severity,
    Verdict,
    VerificationReport,
)

REASON_CONVERGED: Final = "approve_with_gates_pass"
REASON_ANTI_SYCOPHANCY: Final = "anti_sycophancy_forced_review"


@dataclass(frozen=True, slots=True)
class DecidingInputs:
    """Снимок раунда N, собранный w-runtime из артефактов (DESIGN-004)."""

    round: int
    mode: Mode
    review: Review
    verification: VerificationReport
    carried_issues: tuple[Issue, ...]
    patch_current: str
    patch_two_back: str | None
    issue_history: Mapping[int, tuple[Issue, ...]]
    budget_used: BudgetUsed
    limits: Limits


@dataclass(frozen=True, slots=True)
class DecisionDraft:
    """Исход `decide()` — чистое значение, до материализации `Decision`."""

    outcome: Outcome
    reason: str
    open_issues_carried: tuple[str, ...]
    next_round_directive: str | None
    forced_review: bool


def _gates_pass(inputs: DecidingInputs) -> bool:
    """`overall == pass`, либо `analyze` без гейтов (§5.1)."""
    if inputs.verification.overall is OverallStatus.PASS:
        return True
    return inputs.mode is Mode.ANALYZE and not inputs.verification.gates


def _no_carried_blocker(inputs: DecidingInputs) -> bool:
    """В `carried_issues` нет ни одного `blocker` (§5.1)."""
    return not any(
        issue.severity is Severity.BLOCKER for issue in inputs.carried_issues
    )


def _passes_anti_sycophancy(inputs: DecidingInputs) -> bool:
    """Раунд 1: approve засчитывается только `analyze` без правок кода [REQ-008]."""
    if inputs.round != 1:
        return True
    return inputs.mode is Mode.ANALYZE and not inputs.patch_current.strip()


def is_converged(inputs: DecidingInputs) -> bool:
    """Критерий CONVERGED §5.1, включая анти-сикофантию раунда 1 [REQ-007/008]."""
    if inputs.review.verdict is not Verdict.APPROVE:
        return False
    if not _gates_pass(inputs):
        return False
    if not _no_carried_blocker(inputs):
        return False
    return _passes_anti_sycophancy(inputs)
