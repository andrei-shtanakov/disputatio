"""Тесты `DecidingInputs`/`DecisionDraft` и предиката converged: TASK-006,
[DESIGN-004], [DESIGN-005], [REQ-007], [REQ-008].

Импорты `disputatio.core.deciding` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор (`test_deciding_inputs_is_frozen`) превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
"""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from disputatio.contracts import (
    BudgetUsed,
    DiffStats,
    GateResult,
    GateStatus,
    Issue,
    Limits,
    Mode,
    OverallStatus,
    Review,
    Role,
    Severity,
    Verdict,
    VerificationReport,
)


def make_review(
    *, verdict: Verdict = Verdict.APPROVE, issues: list[Issue] | None = None
) -> Review:
    return Review(
        round=1,
        role=Role.REVIEWER,
        verdict=verdict,
        confidence=0.9,
        issues=[] if issues is None else issues,
        checked=["a.py"],
        summary="ok",
    )


def make_verification(
    *,
    overall: OverallStatus = OverallStatus.PASS,
    gates: list[GateResult] | None = None,
) -> VerificationReport:
    default_gates = [GateResult(name="tests", cmd="pytest -q", status=GateStatus.PASS)]
    return VerificationReport(
        round=1,
        gates=default_gates if gates is None else gates,
        overall=overall,
        diff_stats=DiffStats(files=1, insertions=1, deletions=0),
    )


def make_limits(*, max_rounds: int = 8) -> Limits:
    return Limits(
        max_rounds=max_rounds,
        max_total_tokens=1_000_000,
        max_wall_seconds=3600,
        schema_retries=2,
    )


def make_inputs(
    *,
    round: int = 2,
    mode: Mode = Mode.DEVELOP,
    review: Review | None = None,
    verification: VerificationReport | None = None,
    carried_issues: tuple[Issue, ...] = (),
    patch_current: str = "",
    patch_two_back: str | None = None,
    issue_history: "dict[int, tuple[Issue, ...]] | None" = None,
    budget_used: BudgetUsed | None = None,
    limits: Limits | None = None,
):
    from disputatio.core.deciding import DecidingInputs

    return DecidingInputs(
        round=round,
        mode=mode,
        review=review if review is not None else make_review(),
        verification=verification if verification is not None else make_verification(),
        carried_issues=carried_issues,
        patch_current=patch_current,
        patch_two_back=patch_two_back,
        issue_history={} if issue_history is None else issue_history,
        budget_used=BudgetUsed() if budget_used is None else budget_used,
        limits=make_limits() if limits is None else limits,
    )


def test_deciding_inputs_is_frozen() -> None:
    """Присваивание полю `DecidingInputs` бросает `FrozenInstanceError`."""
    try:
        inputs = make_inputs()
    except ImportError as exc:  # red-фаза: deciding.py ещё не создан
        raise AssertionError("src/disputatio/core/deciding.py ещё не создан") from exc

    with pytest.raises(FrozenInstanceError):
        inputs.round = 3  # type: ignore[misc]


def test_decision_draft_is_frozen() -> None:
    """Присваивание полю `DecisionDraft` бросает `FrozenInstanceError`."""
    from disputatio.core.deciding import DecisionDraft

    from disputatio.contracts import Outcome

    draft = DecisionDraft(
        outcome=Outcome.CONTINUE,
        reason="whatever",
        open_issues_carried=(),
        next_round_directive="fix the bug",
        forced_review=False,
    )

    with pytest.raises(FrozenInstanceError):
        draft.reason = "other"  # type: ignore[misc]


def test_deciding_inputs_does_not_allow_mutating_nested_review() -> None:
    """Вложенный `Review` внутри `DecidingInputs` остаётся frozen-моделью."""
    inputs = make_inputs()

    with pytest.raises(ValidationError):
        inputs.review.verdict = Verdict.REJECT  # type: ignore[misc]


def test_converged_true_for_approve_pass_no_blocker_round_two() -> None:
    """approve+pass+carried без blocker, раунд >= 2 → converged истина [REQ-007]."""
    from disputatio.core.deciding import is_converged

    inputs = make_inputs(
        round=2,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.PASS),
        carried_issues=(),
    )

    assert is_converged(inputs) is True


def test_converged_false_when_verification_overall_fail() -> None:
    """approve+fail → converged ложь; fail никогда не проходит критерий [REQ-007]."""
    from disputatio.core.deciding import is_converged

    inputs = make_inputs(
        round=2,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.FAIL),
    )

    assert is_converged(inputs) is False


def test_converged_true_for_analyze_with_empty_gates() -> None:
    """analyze + пустые gates + approve → converged истина [REQ-007]."""
    from disputatio.core.deciding import is_converged

    inputs = make_inputs(
        round=2,
        mode=Mode.ANALYZE,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.FAIL, gates=[]),
    )

    assert is_converged(inputs) is True


def test_converged_false_when_carried_issue_is_blocker() -> None:
    """approve+pass, но carried blocker → converged ложь [REQ-007]."""
    from disputatio.core.deciding import is_converged

    blocker = Issue(id="i1", severity=Severity.BLOCKER, file="a.py", claim="boom")
    inputs = make_inputs(
        round=2,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.PASS),
        carried_issues=(blocker,),
    )

    assert is_converged(inputs) is False


def test_anti_sycophancy_round_one_develop_approve_does_not_trigger() -> None:
    """Раунд 1, mode=develop, approve → критерий converged не срабатывает [REQ-008]."""
    from disputatio.core.deciding import is_converged

    inputs = make_inputs(
        round=1,
        mode=Mode.DEVELOP,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.PASS),
        patch_current="+line\n",
    )

    assert is_converged(inputs) is False


def test_anti_sycophancy_round_one_analyze_empty_patch_converges() -> None:
    """Раунд 1, mode=analyze, пустой patch, approve → converged истина [REQ-008]."""
    from disputatio.core.deciding import is_converged

    inputs = make_inputs(
        round=1,
        mode=Mode.ANALYZE,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.PASS),
        patch_current="   ",
    )

    assert is_converged(inputs) is True


def test_anti_sycophancy_round_one_analyze_nonempty_patch_does_not_converge() -> None:
    """Раунд 1, mode=analyze, непустой patch, approve → converged ложь [REQ-008]."""
    from disputatio.core.deciding import is_converged

    inputs = make_inputs(
        round=1,
        mode=Mode.ANALYZE,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.PASS),
        patch_current="+line\n",
    )

    assert is_converged(inputs) is False


def test_anti_sycophancy_round_two_approve_converges() -> None:
    """Раунд 2, approve, условия §5.1 выполнены → converged истина [REQ-008]."""
    from disputatio.core.deciding import is_converged

    inputs = make_inputs(
        round=2,
        mode=Mode.DEVELOP,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.PASS),
        patch_current="+line\n",
    )

    assert is_converged(inputs) is True


def test_reason_converged_is_pinned() -> None:
    """`REASON_CONVERGED == "approve_with_gates_pass"` (DESIGN-005)."""
    from disputatio.core.deciding import REASON_CONVERGED

    assert REASON_CONVERGED == "approve_with_gates_pass"


def test_reason_anti_sycophancy_is_pinned() -> None:
    """`REASON_ANTI_SYCOPHANCY == "anti_sycophancy_forced_review"` (DESIGN-005)."""
    from disputatio.core.deciding import REASON_ANTI_SYCOPHANCY

    assert REASON_ANTI_SYCOPHANCY == "anti_sycophancy_forced_review"
