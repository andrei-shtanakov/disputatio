"""Тесты модели session.json: TASK-002, [DESIGN-002], [REQ-002].

Импорты `disputatio.contracts.session` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор (`test_spec_4_1_example_validates`) превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
"""

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError


def spec_4_1_example() -> dict[str, Any]:
    """Пример SessionState из SPEC-001 §4.1 (конкретизированы плейсхолдеры)."""
    return {
        "schema": "disputatio/v1",
        "session_id": "0f9b7c2e-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
        "created_at": "2026-08-08T12:00:00+00:00",
        "state": "REVIEWING",
        "current_round": 3,
        "task": {
            "prompt": "текст пользователя",
            "attachments": ["path"],
            "mode": "develop",
        },
        "agents": {
            "author": {
                "adapter": "claude_code",
                "model": "claude-fable-5",
                "session_ref": "cli session id",
            },
            "reviewer": {
                "adapter": "codex",
                "model": "gpt-5.4",
                "session_ref": "abc123",
            },
        },
        "limits": {
            "max_rounds": 4,
            "max_total_tokens": 400000,
            "max_wall_seconds": 1800,
            "schema_retries": 2,
        },
        "budget_used": {
            "tokens": 123456,
            "wall_seconds": 480,
            "cost_usd_est": 1.87,
        },
    }


def test_spec_4_1_example_validates() -> None:
    """Пример §4.1 валидируется дословно из фикстуры-словаря."""
    try:
        from disputatio.contracts.session import SessionState
    except ImportError as exc:  # red-фаза: session.py ещё не создан
        raise AssertionError(
            "src/disputatio/contracts/session.py ещё не создан"
        ) from exc

    state = SessionState.model_validate(spec_4_1_example())
    assert state.session_id == "0f9b7c2e-1a2b-4c3d-8e4f-5a6b7c8d9e0f"
    assert state.state == "REVIEWING"
    assert state.current_round == 3
    assert state.task.mode == "develop"
    assert state.limits.max_rounds == 4
    assert state.budget_used.tokens == 123456


def test_invalid_state_rejected() -> None:
    """Недопустимое значение `state` отклоняется ValidationError."""
    from disputatio.contracts.session import SessionState

    payload = copy.deepcopy(spec_4_1_example())
    payload["state"] = "MEDITATING"
    with pytest.raises(ValidationError):
        SessionState.model_validate(payload)


def test_invalid_mode_rejected() -> None:
    """Недопустимое значение `task.mode` отклоняется ValidationError."""
    from disputatio.contracts.session import SessionState

    payload = copy.deepcopy(spec_4_1_example())
    payload["task"]["mode"] = "chat"
    with pytest.raises(ValidationError):
        SessionState.model_validate(payload)


def test_round_trip_with_schema_key() -> None:
    """Round-trip через JSON: ключ `"schema"` присутствует, модель равна."""
    from disputatio.contracts.session import SessionState

    original = SessionState.model_validate(spec_4_1_example())
    dumped = original.model_dump_json(by_alias=True)
    assert json.loads(dumped)["schema"] == "disputatio/v1"
    assert SessionState.model_validate_json(dumped) == original


def test_session_phase_covers_state_machine() -> None:
    """SessionPhase несёт все 12 состояний машины §2."""
    from disputatio.contracts.session import SessionPhase

    assert {phase.value for phase in SessionPhase} == {
        "IDLE",
        "PROPOSING",
        "VERIFYING",
        "REVIEWING",
        "DECIDING",
        "CONVERGED",
        "DEADLOCK",
        "BUDGET_HIT",
        "ESCALATED",
        "EXPORTING",
        "DONE",
        "FAILED",
    }


def test_budget_used_defaults() -> None:
    """Дефолты BudgetUsed: tokens=0, wall_seconds=0.0, cost_usd_est=0.0."""
    from disputatio.contracts.session import BudgetUsed

    budget = BudgetUsed()
    assert budget.tokens == 0
    assert budget.wall_seconds == 0.0
    assert budget.cost_usd_est == 0.0


def test_agent_ref_session_ref_none_allowed() -> None:
    """AgentRef без session_ref валиден: дефолт None."""
    from disputatio.contracts.session import AgentRef

    agent = AgentRef(adapter="claude_code", model="claude-fable-5")
    assert agent.session_ref is None


def test_counters_updated_via_model_copy() -> None:
    """frozen не ломает write-ahead: счётчики обновляются model_copy(update=...)."""
    from disputatio.contracts.session import BudgetUsed, SessionState

    original = SessionState.model_validate(spec_4_1_example())
    spent = BudgetUsed(tokens=200000, wall_seconds=600.0, cost_usd_est=2.5)
    updated = original.model_copy(update={"budget_used": spent})
    assert updated.budget_used == spent
    assert original.budget_used.tokens == 123456
    assert updated.session_id == original.session_id
