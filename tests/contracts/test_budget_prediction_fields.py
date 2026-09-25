"""Поля прогноза бюджета в контрактах: §4.1 и §4.5 SPEC-001.

`BudgetUsed` получил `unreported_turns` и `tracked_from_start`, `Decision` —
`budget_snapshot`. Все три — расширение `disputatio/v1`: артефакт прежней
версии обязан читаться, а недостающее — читаться так, чтобы прежний учёт в
наблюдения прогноза не попал (`tracked_from_start == False`, снимка нет).
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from disputatio.contracts import BudgetUsed, Decision, Mode, Outcome, SessionState
from disputatio.runtime.config import AgentConfig, LimitsConfig, RuntimeConfig

_OLD_SESSION = {
    "schema": "disputatio/v1",
    "session_id": "s-old",
    "created_at": "2026-08-10T09:00:00Z",
    "state": "DECIDING",
    "current_round": 2,
    "task": {"prompt": "задача", "attachments": [], "mode": "develop"},
    "agents": {
        "author": {"adapter": "claude_code", "model": "opus"},
        "reviewer": {"adapter": "codex", "model": "gpt"},
    },
    "limits": {
        "max_rounds": 4,
        "max_total_tokens": 400000,
        "max_wall_seconds": 1800,
        "schema_retries": 2,
    },
    "budget_used": {"tokens": 1234, "wall_seconds": 48.0, "cost_usd_est": 0.5},
}

_OLD_DECISION = {
    "schema": "disputatio/v1",
    "round": 2,
    "outcome": "continue",
    "reason": "continue_revise_cycle",
    "open_issues_carried": ["R2-1"],
    "next_round_directive": "поправить",
}


def test_old_session_json_reads_with_untracked_zero_counters() -> None:
    """`session.json` прежней версии: счётчик 0, признак `false` (§4.1)."""
    state = SessionState.model_validate_json(json.dumps(_OLD_SESSION))

    assert state.budget_used.unreported_turns == 0
    assert state.budget_used.tracked_from_start is False
    assert state.budget_used.tokens == 1234


def test_budget_used_defaults_are_untracked() -> None:
    """Конструктор без аргументов — «прежний учёт»: новую сессию помечает бутстрап."""
    used = BudgetUsed()

    assert used.unreported_turns == 0
    assert used.tracked_from_start is False


def test_unreported_turns_cannot_be_negative() -> None:
    """Счётчик вызовов без отчёта неотрицателен, как и `tokens`."""
    with pytest.raises(ValidationError):
        BudgetUsed(unreported_turns=-1)


def test_new_session_is_tracked_from_start() -> None:
    """Бутстрап новой сессии ставит `tracked_from_start = true` (§4.1)."""
    config = RuntimeConfig(
        session_id="s-new",
        task_prompt="задача",
        mode=Mode.DEVELOP,
        author=AgentConfig(adapter="claude_code", model="opus"),
        reviewer=AgentConfig(adapter="codex", model="gpt"),
        limits=LimitsConfig(
            max_rounds=4,
            max_total_tokens=400_000,
            max_wall_seconds=1800,
            schema_retries=2,
        ),
        gates=(),
        base_commit="0" * 40,
    )

    state = config.to_session_state(created_at=datetime(2026, 9, 1, tzinfo=UTC))

    assert state.budget_used.tracked_from_start is True
    assert state.budget_used.unreported_turns == 0
    assert state.budget_used.tokens == 0


def test_old_decision_json_reads_without_snapshot() -> None:
    """`decision.json` прежней версии читается; снимка у него нет (§4.5)."""
    decision = Decision.model_validate_json(json.dumps(_OLD_DECISION))

    assert decision.budget_snapshot is None
    assert decision.outcome is Outcome.CONTINUE


def test_decision_snapshot_round_trips() -> None:
    """Снимок пишется и читается тремя полями §4.5."""
    payload = {
        **_OLD_DECISION,
        "budget_snapshot": {"tokens": 10, "wall_seconds": 2.5, "unreported_turns": 1},
    }

    decision = Decision.model_validate_json(json.dumps(payload))

    assert decision.budget_snapshot is not None
    assert decision.budget_snapshot.tokens == 10
    assert decision.budget_snapshot.wall_seconds == 2.5
    assert decision.budget_snapshot.unreported_turns == 1
    again = Decision.model_validate_json(decision.model_dump_json(by_alias=True))
    assert again == decision


def test_decision_snapshot_rejects_foreign_fields() -> None:
    """Снимок — ровно три поля; `cost_usd_est` в него не входит (`extra=forbid`)."""
    payload = {
        **_OLD_DECISION,
        "budget_snapshot": {
            "tokens": 10,
            "wall_seconds": 2.5,
            "unreported_turns": 0,
            "cost_usd_est": 0.1,
        },
    }

    with pytest.raises(ValidationError):
        Decision.model_validate_json(json.dumps(payload))


def test_default_counters_are_not_serialized() -> None:
    """Умолчания не пишутся: агрегат манифеста пайплайна остаётся трёхпольным.

    SPEC-002 §4.2 держит у `budget_used` манифеста ровно три поля, а модель у
    него та же. Записанное значение, отличное от умолчания, при этом
    обязано доехать до диска — иначе признак новой сессии терялся бы на
    первом же `save`.
    """
    plain = json.loads(BudgetUsed(tokens=5).model_dump_json())
    tracked = json.loads(
        BudgetUsed(unreported_turns=2, tracked_from_start=True).model_dump_json()
    )

    assert set(plain) == {"tokens", "wall_seconds", "cost_usd_est"}
    assert tracked["unreported_turns"] == 2
    assert tracked["tracked_from_start"] is True
