"""Снимок входов `DECIDING`, критерий converged и `decide()` §5 (DESIGN-004/005).

`DecidingInputs`/`DecisionDraft` — frozen-датаклассы: значения, а не объекты
с поведением. `is_converged()` реализует критерий сходимости [REQ-007] и
анти-сикофантию [REQ-008] в одном месте — по DESIGN-005 защита раунда 1
живёт внутри критерия converged, не как отдельная ветка. `decide()`
реализует строгий top-down порядок §5 [REQ-006]: converged → budget_hit
[REQ-010] → осцилляция [REQ-011] → max_rounds [REQ-012] → иначе continue,
линейной цепочкой ранних `return` без таблиц приоритетов.
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
from disputatio.core.oscillation import (
    OSCILLATION_DIFF_THRESHOLD,
    find_repeated_issue,
    patch_similarity,
)

REASON_CONVERGED: Final = "approve_with_gates_pass"
REASON_ANTI_SYCOPHANCY: Final = "anti_sycophancy_forced_review"
REASON_BUDGET_TOKENS: Final = "budget_hit: tokens"
REASON_BUDGET_WALL: Final = "budget_hit: wall_seconds"
REASON_OSCILLATION_DIFF: Final = "oscillation: diff-similarity"
REASON_OSCILLATION_ISSUE: Final = "oscillation: repeated issue"
REASON_MAX_ROUNDS: Final = "max_rounds"
REASON_CONTINUE: Final = "continue_revise_cycle"

_ANTI_SYCOPHANCY_DIRECTIVE: Final = (
    "Раунд 1 принят только для analyze без правок кода; требуется один "
    "содержательный цикл ревью: минимум 3 замечания любой severity либо "
    "явное обоснование в checked, почему их нет."
)


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


def _converged_except_anti_sycophancy(inputs: DecidingInputs) -> bool:
    """approve+gates_pass+no_carried_blocker, без учёта анти-сикофантии (§5.1)."""
    if inputs.review.verdict is not Verdict.APPROVE:
        return False
    if not _gates_pass(inputs):
        return False
    return _no_carried_blocker(inputs)


def is_converged(inputs: DecidingInputs) -> bool:
    """Критерий CONVERGED §5.1, включая анти-сикофантию раунда 1 [REQ-007/008]."""
    return _converged_except_anti_sycophancy(inputs) and _passes_anti_sycophancy(inputs)


def _anti_sycophancy_blocked(inputs: DecidingInputs) -> bool:
    """True, если converged не сработал именно из-за анти-сикофантии раунда 1."""
    return _converged_except_anti_sycophancy(inputs) and not _passes_anti_sycophancy(
        inputs
    )


def _budget_hit_reason(inputs: DecidingInputs) -> str | None:
    """`REASON_BUDGET_TOKENS`/`REASON_BUDGET_WALL` при строгом превышении
    лимита [REQ-010].
    """
    if inputs.budget_used.tokens > inputs.limits.max_total_tokens:
        return REASON_BUDGET_TOKENS
    if inputs.budget_used.wall_seconds > inputs.limits.max_wall_seconds:
        return REASON_BUDGET_WALL
    return None


def _oscillation_reason(inputs: DecidingInputs) -> str | None:
    """Diff-similarity, затем repeated-issue, в этом порядке (DESIGN-006) [REQ-011].

    Diff-similarity пропускается для `analyze`: в этом режиме `patch_current`
    всегда пуст, а `patch_similarity("", "") == 1.0` (два пустых патча
    считаются идентичными по определению) ложно триггернуло бы осцилляцию
    на каждой analyze-сессии, дошедшей до раунда 3, хотя правки кода там в
    принципе не производятся — `_gates_pass` для `analyze` делает такое же
    исключение.
    """
    if inputs.mode is not Mode.ANALYZE and inputs.patch_two_back is not None:
        similarity = patch_similarity(inputs.patch_current, inputs.patch_two_back)
        if similarity > OSCILLATION_DIFF_THRESHOLD:
            return REASON_OSCILLATION_DIFF
    repeated = find_repeated_issue(inputs.review.issues, inputs.issue_history)
    if repeated is not None:
        return f"{REASON_OSCILLATION_ISSUE}: {repeated.file}/{repeated.id}"
    return None


def _build_directive(review: Review) -> str:
    """Директива автору следующего раунда, собранная из issues ревью (§5)."""
    if not review.issues:
        return "Продолжить работу над задачей: цикл ревью продолжается."
    return "; ".join(f"{issue.file}: {issue.claim}" for issue in review.issues)


def decide(inputs: DecidingInputs) -> DecisionDraft:
    """`DECIDING` §5: converged → budget_hit → осцилляция → max_rounds → continue.

    Строгий top-down порядок [REQ-006] — линейная цепочка ранних `return`,
    первое сработавшее условие терминально.
    """
    open_issues_carried = tuple(issue.id for issue in inputs.carried_issues)

    if is_converged(inputs):
        return DecisionDraft(
            outcome=Outcome.CONVERGED,
            reason=REASON_CONVERGED,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
        )

    budget_reason = _budget_hit_reason(inputs)
    if budget_reason is not None:
        return DecisionDraft(
            outcome=Outcome.BUDGET_HIT,
            reason=budget_reason,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
        )

    oscillation_reason = _oscillation_reason(inputs)
    if oscillation_reason is not None:
        return DecisionDraft(
            outcome=Outcome.DEADLOCK,
            reason=oscillation_reason,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
        )

    if inputs.round >= inputs.limits.max_rounds:
        return DecisionDraft(
            outcome=Outcome.DEADLOCK,
            reason=REASON_MAX_ROUNDS,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
        )

    if _anti_sycophancy_blocked(inputs):
        return DecisionDraft(
            outcome=Outcome.CONTINUE,
            reason=REASON_ANTI_SYCOPHANCY,
            open_issues_carried=open_issues_carried,
            next_round_directive=_ANTI_SYCOPHANCY_DIRECTIVE,
            forced_review=True,
        )

    return DecisionDraft(
        outcome=Outcome.CONTINUE,
        reason=REASON_CONTINUE,
        open_issues_carried=open_issues_carried,
        next_round_directive=_build_directive(inputs.review),
        forced_review=False,
    )
