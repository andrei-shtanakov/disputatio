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
    Outcome,
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
    from disputatio.contracts import Outcome
    from disputatio.core.deciding import DecisionDraft

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


def make_request_changes_review(
    *, issue_file: str = "a.py", claim: str = "off by one"
) -> Review:
    return make_review(
        verdict=Verdict.REQUEST_CHANGES,
        issues=[
            Issue(
                id="i1",
                severity=Severity.MAJOR,
                file=issue_file,
                claim=claim,
                evidence="see diff",
            )
        ],
    )


def test_decide_converged_wins_over_budget_hit() -> None:
    """converged и budget_hit истинны одновременно → CONVERGED побеждает [REQ-006]."""
    try:
        from disputatio.core.deciding import decide
    except ImportError as exc:  # red-фаза: decide() ещё не реализована
        raise AssertionError("decide() ещё не реализована в core/deciding.py") from exc

    inputs = make_inputs(
        round=2,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.PASS),
        carried_issues=(),
        budget_used=BudgetUsed(tokens=2_000_000, wall_seconds=1.0),
        limits=make_limits(max_rounds=8),
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONVERGED


def test_decide_budget_hit_wins_over_oscillation() -> None:
    """budget_hit и осцилляция истинны одновременно → BUDGET_HIT побеждает [REQ-006]."""
    from disputatio.core.deciding import decide

    patch = "@@ -1,2 +1,2 @@\n+line one\n-line two\n"
    inputs = make_inputs(
        round=3,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        budget_used=BudgetUsed(tokens=2_000_000, wall_seconds=1.0),
        limits=make_limits(max_rounds=8),
        patch_current=patch,
        patch_two_back=patch,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.BUDGET_HIT


def test_decide_oscillation_wins_over_max_rounds() -> None:
    """Осцилляция и max_rounds истинны одновременно → DEADLOCK с причиной осцилляции [REQ-006]."""
    from disputatio.core.deciding import REASON_OSCILLATION_DIFF, decide

    patch = "@@ -1,2 +1,2 @@\n+line one\n-line two\n"
    inputs = make_inputs(
        round=4,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        budget_used=BudgetUsed(),
        limits=make_limits(max_rounds=4),
        patch_current=patch,
        patch_two_back=patch,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.DEADLOCK
    assert draft.reason == REASON_OSCILLATION_DIFF


def test_decide_continue_when_nothing_triggers() -> None:
    """Ни одно условие не сработало → CONTINUE [REQ-006]."""
    from disputatio.core.deciding import decide

    inputs = make_inputs(
        round=2,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        budget_used=BudgetUsed(),
        limits=make_limits(max_rounds=8),
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE


def test_decide_budget_boundary_tokens_equal_limit_does_not_trigger() -> None:
    """`tokens == max_total_tokens` (граница) → budget_hit не срабатывает [REQ-010]."""
    from disputatio.core.deciding import decide

    limits = make_limits(max_rounds=8)
    inputs = make_inputs(
        round=2,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        budget_used=BudgetUsed(tokens=limits.max_total_tokens, wall_seconds=1.0),
        limits=limits,
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE


def test_decide_budget_hit_tokens_over_limit() -> None:
    """`tokens > max_total_tokens` → `BUDGET_HIT`/`REASON_BUDGET_TOKENS` [REQ-010]."""
    from disputatio.core.deciding import REASON_BUDGET_TOKENS, decide

    limits = make_limits(max_rounds=8)
    inputs = make_inputs(
        round=2,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        budget_used=BudgetUsed(tokens=limits.max_total_tokens + 1, wall_seconds=1.0),
        limits=limits,
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.BUDGET_HIT
    assert draft.reason == REASON_BUDGET_TOKENS


def test_decide_budget_hit_wall_seconds_over_limit() -> None:
    """`wall_seconds > max_wall_seconds` → `BUDGET_HIT`/`REASON_BUDGET_WALL` [REQ-010]."""
    from disputatio.core.deciding import REASON_BUDGET_WALL, decide

    limits = make_limits(max_rounds=8)
    inputs = make_inputs(
        round=2,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        budget_used=BudgetUsed(
            tokens=1, wall_seconds=float(limits.max_wall_seconds) + 1.0
        ),
        limits=limits,
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.BUDGET_HIT
    assert draft.reason == REASON_BUDGET_WALL


def test_decide_oscillation_diff_similarity_triggers_deadlock() -> None:
    """`similarity > 0.8` при доступном `patch_two_back` → `DEADLOCK`/`REASON_OSCILLATION_DIFF` [REQ-011]."""
    from disputatio.core.deciding import REASON_OSCILLATION_DIFF, decide

    patch = "@@ -1,2 +1,2 @@\n+line one\n-line two\n"
    inputs = make_inputs(
        round=3,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=8),
        patch_current=patch,
        patch_two_back=patch,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.DEADLOCK
    assert draft.reason == REASON_OSCILLATION_DIFF


def test_decide_oscillation_diff_similarity_exactly_threshold_does_not_trigger() -> (
    None
):
    """Similarity ровно `0.8` → осцилляция по diff не срабатывает [REQ-011]."""
    from disputatio.core.deciding import decide

    inputs = make_inputs(
        round=3,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=8),
        patch_current="@@ -1,4 +1,4 @@\n+one\n+two\n+three\n+four\n",
        patch_two_back="@@ -1,5 +1,5 @@\n+one\n+two\n+three\n+four\n+five\n",
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE


def test_decide_oscillation_diff_skipped_when_patch_two_back_is_none() -> None:
    """`patch_two_back=None` (раунд < 3) → проверка diff-similarity пропускается [REQ-011]."""
    from disputatio.core.deciding import decide

    inputs = make_inputs(
        round=2,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=8),
        patch_current="+line one\n-line two\n",
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE


def test_decide_oscillation_repeated_issue_third_time_triggers_deadlock() -> None:
    """Issue открыто в 3-й раз (тот же file, нечёткое совпадение claim) → `DEADLOCK`/`REASON_OSCILLATION_ISSUE` [REQ-011]."""
    from disputatio.core.deciding import REASON_OSCILLATION_ISSUE, decide

    current_issue = Issue(
        id="i3",
        severity=Severity.MAJOR,
        file="a.py",
        claim="Off by one error in loop",
        evidence="see diff",
    )
    history: dict[int, tuple[Issue, ...]] = {
        1: (
            Issue(
                id="i1",
                severity=Severity.MAJOR,
                file="a.py",
                claim="off by one error in loop",
                evidence="see diff",
            ),
        ),
        2: (
            Issue(
                id="i2",
                severity=Severity.MAJOR,
                file="a.py",
                claim="Off  by one  error in loop",
                evidence="see diff",
            ),
        ),
    }
    inputs = make_inputs(
        round=3,
        review=make_review(verdict=Verdict.REQUEST_CHANGES, issues=[current_issue]),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=8),
        patch_two_back=None,
        issue_history=history,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.DEADLOCK
    assert draft.reason.startswith(REASON_OSCILLATION_ISSUE)
    assert "a.py" in draft.reason
    assert "i3" in draft.reason


def test_decide_oscillation_repeated_issue_second_time_does_not_trigger() -> None:
    """Issue, совпавшее лишь с одним прошлым раундом (2-е открытие), не триггерит [REQ-011]."""
    from disputatio.core.deciding import decide

    current_issue = Issue(
        id="i2",
        severity=Severity.MAJOR,
        file="a.py",
        claim="Off by one error in loop",
        evidence="see diff",
    )
    history: dict[int, tuple[Issue, ...]] = {
        1: (
            Issue(
                id="i1",
                severity=Severity.MAJOR,
                file="a.py",
                claim="off by one error in loop",
                evidence="see diff",
            ),
        ),
    }
    inputs = make_inputs(
        round=2,
        review=make_review(verdict=Verdict.REQUEST_CHANGES, issues=[current_issue]),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=8),
        patch_two_back=None,
        issue_history=history,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE


def test_decide_max_rounds_deadlock_when_round_equals_max_rounds() -> None:
    """`round == max_rounds` без сходимости → `DEADLOCK`/`REASON_MAX_ROUNDS` [REQ-012]."""
    from disputatio.core.deciding import REASON_MAX_ROUNDS, decide

    inputs = make_inputs(
        round=4,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=4),
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.DEADLOCK
    assert draft.reason == REASON_MAX_ROUNDS


def test_decide_continue_when_round_below_max_rounds() -> None:
    """`round < max_rounds` и ничего не сработало → `CONTINUE` [REQ-012]."""
    from disputatio.core.deciding import decide

    inputs = make_inputs(
        round=3,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=4),
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE


def test_decide_continue_when_verification_fails_and_review_requests_changes() -> None:
    """`overall=fail`+`request_changes` не блокирует ревью-цикл → `CONTINUE` [REQ-009]."""
    from disputatio.core.deciding import decide

    inputs = make_inputs(
        round=2,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=8),
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE


def test_decide_anti_sycophancy_round_one_forces_continue() -> None:
    """Раунд 1, develop, approve, иначе все условия CONVERGED истинны → `CONTINUE`/`REASON_ANTI_SYCOPHANCY`/`forced_review=True` [REQ-008]."""
    from disputatio.core.deciding import REASON_ANTI_SYCOPHANCY, decide

    inputs = make_inputs(
        round=1,
        mode=Mode.DEVELOP,
        review=make_review(verdict=Verdict.APPROVE),
        verification=make_verification(overall=OverallStatus.PASS),
        carried_issues=(),
        patch_current="+line\n",
        limits=make_limits(max_rounds=8),
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE
    assert draft.reason == REASON_ANTI_SYCOPHANCY
    assert draft.forced_review is True


def test_decide_continue_has_nonempty_next_round_directive() -> None:
    """`CONTINUE` ⇒ `next_round_directive` — непустая строка."""
    from disputatio.core.deciding import decide

    inputs = make_inputs(
        round=2,
        review=make_request_changes_review(),
        verification=make_verification(overall=OverallStatus.FAIL),
        limits=make_limits(max_rounds=8),
        patch_two_back=None,
    )

    draft = decide(inputs)

    assert draft.outcome is Outcome.CONTINUE
    assert isinstance(draft.next_round_directive, str)
    assert draft.next_round_directive.strip() != ""


def test_decide_terminal_outcomes_have_none_directive() -> None:
    """Терминальные исходы (`CONVERGED`/`BUDGET_HIT`/`DEADLOCK`) ⇒ `next_round_directive is None`."""
    from disputatio.core.deciding import decide

    converged = decide(
        make_inputs(
            round=2,
            review=make_review(verdict=Verdict.APPROVE),
            verification=make_verification(overall=OverallStatus.PASS),
            carried_issues=(),
            limits=make_limits(max_rounds=8),
        )
    )
    budget_hit = decide(
        make_inputs(
            round=2,
            review=make_request_changes_review(),
            verification=make_verification(overall=OverallStatus.FAIL),
            budget_used=BudgetUsed(tokens=2_000_000, wall_seconds=1.0),
            limits=make_limits(max_rounds=8),
            patch_two_back=None,
        )
    )
    deadlock = decide(
        make_inputs(
            round=4,
            review=make_request_changes_review(),
            verification=make_verification(overall=OverallStatus.FAIL),
            limits=make_limits(max_rounds=4),
            patch_two_back=None,
        )
    )

    assert converged.next_round_directive is None
    assert budget_hit.next_round_directive is None
    assert deadlock.next_round_directive is None


def test_reason_budget_tokens_is_pinned() -> None:
    """`REASON_BUDGET_TOKENS == "budget_hit: tokens"` (DESIGN-005)."""
    from disputatio.core.deciding import REASON_BUDGET_TOKENS

    assert REASON_BUDGET_TOKENS == "budget_hit: tokens"


def test_reason_budget_wall_is_pinned() -> None:
    """`REASON_BUDGET_WALL == "budget_hit: wall_seconds"` (DESIGN-005)."""
    from disputatio.core.deciding import REASON_BUDGET_WALL

    assert REASON_BUDGET_WALL == "budget_hit: wall_seconds"


def test_reason_oscillation_diff_is_pinned() -> None:
    """`REASON_OSCILLATION_DIFF == "oscillation: diff-similarity"` (DESIGN-005)."""
    from disputatio.core.deciding import REASON_OSCILLATION_DIFF

    assert REASON_OSCILLATION_DIFF == "oscillation: diff-similarity"


def test_reason_oscillation_issue_is_pinned() -> None:
    """`REASON_OSCILLATION_ISSUE == "oscillation: repeated issue"` (DESIGN-005)."""
    from disputatio.core.deciding import REASON_OSCILLATION_ISSUE

    assert REASON_OSCILLATION_ISSUE == "oscillation: repeated issue"


def test_reason_max_rounds_is_pinned() -> None:
    """`REASON_MAX_ROUNDS == "max_rounds"` (DESIGN-005)."""
    from disputatio.core.deciding import REASON_MAX_ROUNDS

    assert REASON_MAX_ROUNDS == "max_rounds"
