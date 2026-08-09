"""Тесты инварианта I4: `SessionFsm.handle_schema_invalid`/`handle_step_success`.

TASK-009, [DESIGN-008], [REQ-014].

Счётчик повторов текущего шага — эфемерный (живёт в `SessionFsm`, не в
`SessionState`, ADR-W2-05). `handle_schema_invalid(detail)` инкрементирует
счётчик: пока `count <= limits.schema_retries` → `RetryAction.RETRY` без
касания состояния/портов; при исчерпании — `transition(FAILED)` (write-ahead
+ событие) и `RetryAction.FAILED`. Сброс — либо явный `handle_step_success`,
либо любой успешный `transition()` (новый шаг — новый лимит).

Импорт `RetryAction` и вызов `handle_schema_invalid`/`handle_step_success`
оборачиваются: на момент red-чекпоинта ни enum'а, ни методов ещё нет, а
гейт принимает red только при падении assertion'ом, не `ImportError`/
`AttributeError`.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from disputatio.contracts import SessionPhase, SessionState

from .conftest import FakeEventSink, FakeStateStore, make_session_state

P = SessionPhase


def _retry_action_cls() -> Any:
    """Импортирует `RetryAction`; `ImportError` на red — assertion, не крах."""
    try:
        from disputatio.core.machine import RetryAction
    except ImportError as exc:  # red-фаза: enum'а ещё нет
        raise AssertionError(
            "disputatio.core.machine.RetryAction ещё не создан"
        ) from exc
    return RetryAction


def _make_fsm(
    state: SessionState,
    *,
    store: Any,
    sink: Any,
    now: Callable[[], datetime],
) -> Any:
    from disputatio.core.machine import SessionFsm

    return SessionFsm(state, store=store, sink=sink, now=now)


def _handle_schema_invalid(fsm: Any, detail: str) -> Any:
    """Вызывает `handle_schema_invalid`; `AttributeError` на red — assertion."""
    try:
        method = fsm.handle_schema_invalid
    except AttributeError as exc:  # red-фаза: метода ещё нет
        raise AssertionError(
            "SessionFsm.handle_schema_invalid ещё не реализован"
        ) from exc
    return method(detail)


def _handle_step_success(fsm: Any) -> None:
    """Вызывает `handle_step_success`; `AttributeError` на red — assertion."""
    try:
        method = fsm.handle_step_success
    except AttributeError as exc:  # red-фаза: метода ещё нет
        raise AssertionError(
            "SessionFsm.handle_step_success ещё не реализован"
        ) from exc
    method()


def test_two_schema_invalid_calls_stay_below_limit_and_touch_nothing(
    store: FakeStateStore,
    sink: FakeEventSink,
    calls: list[str],
    fixed_now: Callable[[], datetime],
) -> None:
    """`schema_retries=2`: два повтора подряд — `RETRY`, ничего не тронуто."""
    RetryAction = _retry_action_cls()
    state = make_session_state(state=P.PROPOSING, current_round=1)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    first = _handle_schema_invalid(fsm, "невалидный JSON")
    second = _handle_schema_invalid(fsm, "невалидный JSON снова")

    assert first is RetryAction.RETRY
    assert second is RetryAction.RETRY
    assert fsm.state == state
    assert store.saved == []
    assert sink.events == []
    assert calls == []


def test_third_consecutive_schema_invalid_fails_with_write_ahead(
    store: FakeStateStore,
    sink: FakeEventSink,
    calls: list[str],
    fixed_now: Callable[[], datetime],
) -> None:
    """Третий подряд schema-invalid при `schema_retries=2` → `FAILED`."""
    RetryAction = _retry_action_cls()
    state = make_session_state(state=P.PROPOSING, current_round=1)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    _handle_schema_invalid(fsm, "1")
    _handle_schema_invalid(fsm, "2")
    third = _handle_schema_invalid(fsm, "3")

    assert third is RetryAction.FAILED
    assert fsm.state.state is P.FAILED
    assert calls == ["save(phase=FAILED)", "emit(state_change)"]
    assert sink.events[0].payload == {"from": "PROPOSING", "to": "FAILED"}


def test_handle_step_success_resets_retry_counter(
    store: FakeStateStore,
    sink: FakeEventSink,
    calls: list[str],
    fixed_now: Callable[[], datetime],
) -> None:
    """`handle_step_success` после одного повтора обнуляет счётчик."""
    RetryAction = _retry_action_cls()
    state = make_session_state(state=P.PROPOSING, current_round=1)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    _handle_schema_invalid(fsm, "1")
    _handle_step_success(fsm)

    # Без сброса второй из этих двух вызовов был бы третьим подряд (count=3)
    # и вернул бы FAILED; сброс держит оба в пределах лимита.
    after_reset_first = _handle_schema_invalid(fsm, "2")
    after_reset_second = _handle_schema_invalid(fsm, "3")

    assert after_reset_first is RetryAction.RETRY
    assert after_reset_second is RetryAction.RETRY
    assert calls == []


def test_successful_transition_resets_retry_counter(
    store: FakeStateStore,
    sink: FakeEventSink,
    calls: list[str],
    fixed_now: Callable[[], datetime],
) -> None:
    """Успешный `transition()` тоже обнуляет счётчик повторов."""
    RetryAction = _retry_action_cls()
    state = make_session_state(state=P.PROPOSING, current_round=1)
    fsm = _make_fsm(state, store=store, sink=sink, now=fixed_now)

    _handle_schema_invalid(fsm, "1")
    fsm.transition(P.VERIFYING)
    calls.clear()

    # Без сброса второй из этих двух вызовов был бы третьим подряд (count=3)
    # и вернул бы FAILED; успешный переход держит оба в пределах лимита.
    after_reset_first = _handle_schema_invalid(fsm, "2")
    after_reset_second = _handle_schema_invalid(fsm, "3")

    assert after_reset_first is RetryAction.RETRY
    assert after_reset_second is RetryAction.RETRY
    assert calls == []
