"""Тесты `SessionFsm.apply_decision`/`is_partial`: TASK-008, [DESIGN-007],
[REQ-013], [REQ-003], [REQ-004], [REQ-015].

Проверяется материализация `Decision` (§4.5, schema `disputatio/v1`) и
детерминированная цепочка переходов §5: `CONVERGED` → `DECIDING→CONVERGED→
EXPORTING`; `DEADLOCK`/`BUDGET_HIT` → `DECIDING→{...}→ESCALATED→EXPORTING`
(partial); `CONTINUE` → `DECIDING→PROPOSING`, `current_round+1`, пустая
директива — `ValueError` без единого hop'а. Каждый hop цепочки обязан
соблюдать write-ahead (I2) и ровно одно событие `state_change` — тот же
общий журнал `calls`, что и в `test_machine.py`.

`SessionFsm.apply_decision` на момент red-чекпоинта отсутствует:
`fsm.apply_decision` падает `AttributeError`, не `AssertionError` — гейт
принимает red только по assertion'у, поэтому `_apply` оборачивает вызов.
Аналогично `is_partial` ещё не существует в `disputatio.core.machine` —
`_is_partial` оборачивает `ImportError`.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from disputatio.contracts import Decision, Outcome, SessionPhase, SessionState
from disputatio.core.deciding import DecisionDraft
from disputatio.core.machine import SessionFsm

from .conftest import FakeEventSink, FakeStateStore, make_session_state

P = SessionPhase


def _apply(fsm: SessionFsm, draft: DecisionDraft) -> tuple[Decision, SessionState]:
    """Вызывает `apply_decision`; `AttributeError` на red — assertion, не крах."""
    try:
        method = fsm.apply_decision
    except AttributeError as exc:  # red-фаза: метода ещё нет
        raise AssertionError("SessionFsm.apply_decision ещё не реализован") from exc
    return method(draft)


def _is_partial(outcome: Outcome) -> bool:
    """Вызывает `is_partial`; `ImportError` на red — assertion, не крах."""
    try:
        from disputatio.core.machine import is_partial
    except ImportError as exc:  # red-фаза: функции ещё нет
        raise AssertionError(
            "disputatio.core.machine.is_partial ещё не создан"
        ) from exc
    return is_partial(outcome)


def _make_fsm(
    state: SessionState,
    *,
    store: Any,
    sink: Any,
    now: Callable[[], datetime],
) -> SessionFsm:
    return SessionFsm(state, store=store, sink=sink, now=now)


def _converged_draft() -> DecisionDraft:
    return DecisionDraft(
        outcome=Outcome.CONVERGED,
        reason="approve_with_gates_pass",
        open_issues_carried=(),
        next_round_directive=None,
        forced_review=False,
    )


def _continue_draft(directive: str | None) -> DecisionDraft:
    return DecisionDraft(
        outcome=Outcome.CONTINUE,
        reason="continue_revise_cycle",
        open_issues_carried=("R1",),
        next_round_directive=directive,
        forced_review=False,
    )


def test_converged_produces_valid_decision_and_reaches_exporting(
    store: FakeStateStore,
    sink: FakeEventSink,
    calls: list[str],
    fixed_now: Callable[[], datetime],
) -> None:
    """`CONVERGED`: `Decision` валиден по схеме v1; финальная фаза `EXPORTING`."""
    state = make_session_state(state=P.DECIDING, current_round=2)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    decision, final_state = _apply(fsm, _converged_draft())

    assert decision.schema_ == "disputatio/v1"
    assert decision.round == 2
    assert decision.outcome is Outcome.CONVERGED
    assert final_state.state is P.EXPORTING
    assert fsm.state is final_state
    # save-до-emit на КАЖДЫЙ hop цепочки [REQ-003]/[REQ-004].
    assert calls == [
        "save(phase=CONVERGED)",
        "emit(state_change)",
        "save(phase=EXPORTING)",
        "emit(state_change)",
    ]


@pytest.mark.parametrize(
    "outcome",
    [Outcome.DEADLOCK, Outcome.BUDGET_HIT],
)
def test_deadlock_and_budget_hit_reach_exporting_via_escalated(
    outcome: Outcome,
    store: FakeStateStore,
    sink: FakeEventSink,
    calls: list[str],
    fixed_now: Callable[[], datetime],
) -> None:
    """`DEADLOCK`/`BUDGET_HIT`: цепочка через `ESCALATED` до `EXPORTING`."""
    state = make_session_state(state=P.DECIDING, current_round=5)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)
    draft = DecisionDraft(
        outcome=outcome,
        reason="max_rounds",
        open_issues_carried=(),
        next_round_directive=None,
        forced_review=False,
    )

    decision, final_state = _apply(fsm, draft)

    assert decision.outcome is outcome
    assert final_state.state is P.EXPORTING
    assert calls == [
        f"save(phase={outcome.value})",
        "emit(state_change)",
        "save(phase=ESCALATED)",
        "emit(state_change)",
        "save(phase=EXPORTING)",
        "emit(state_change)",
    ]
    payloads = [event.payload for event in sink.events]
    assert payloads == [
        {"from": "DECIDING", "to": outcome.value},
        {"from": outcome.value, "to": "ESCALATED"},
        {"from": "ESCALATED", "to": "EXPORTING"},
    ]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.DEADLOCK, True),
        (Outcome.BUDGET_HIT, True),
        (Outcome.CONVERGED, False),
        (Outcome.CONTINUE, False),
    ],
)
def test_is_partial_pinned_for_each_outcome(outcome: Outcome, expected: bool) -> None:
    """`is_partial` — `True` только для `DEADLOCK`/`BUDGET_HIT`."""
    assert _is_partial(outcome) is expected


def test_continue_advances_round_and_carries_directive(
    store: FakeStateStore,
    sink: FakeEventSink,
    calls: list[str],
    fixed_now: Callable[[], datetime],
) -> None:
    """`CONTINUE`: `DECIDING→PROPOSING`, `current_round == N+1`, директива жива."""
    state = make_session_state(state=P.DECIDING, current_round=2)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    decision, final_state = _apply(fsm, _continue_draft("исправь тест X"))

    assert final_state.state is P.PROPOSING
    assert final_state.current_round == 3
    # I3 [REQ-015]: Decision несёт round входного раунда N, а не N+1.
    assert decision.round == 2
    assert decision.next_round_directive == "исправь тест X"
    assert calls == ["save(phase=PROPOSING)", "emit(state_change)"]
    assert sink.events[0].round == 3
    # Артефакты раунда N не мутируются: исходный объект state нетронут.
    assert state.current_round == 2
    assert state.state is P.DECIDING


@pytest.mark.parametrize("directive", [None, "", "   "])
def test_continue_without_directive_raises_and_touches_nothing(
    directive: str | None,
    store: FakeStateStore,
    sink: FakeEventSink,
    calls: list[str],
    fixed_now: Callable[[], datetime],
) -> None:
    """Пустая/пробельная директива при `CONTINUE` — `ValueError`, без hop'а (ADR-W2-04)."""
    state = make_session_state(state=P.DECIDING, current_round=2)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    with pytest.raises(ValueError, match="директив"):
        _apply(fsm, _continue_draft(directive))

    assert fsm.state == state
    assert store.saved == []
    assert sink.events == []
    assert calls == []
