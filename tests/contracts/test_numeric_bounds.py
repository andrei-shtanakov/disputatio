"""Тесты TASK-018: числовые границы round/current_round/max_rounds/tokens.

[REQ-018], [DESIGN-016]: `ge`-границы на уровне схемы там, где
отрицательные/нулевые значения бессмысленны — `round >= 1` в раундовых
артефактах (раунды нумеруются с 1, `rounds/001`), `current_round >= 0`
(0 = до первого раунда), `max_rounds >= 1` (лимит < 1 делает loop
невозможным), `budget_used.tokens >= 0` (счётчик), `Event.round` — `None`
либо `>= 1` (вне-раундовые события несут `None`, не `0`). Прочие числовые
поля не трогаются. Новый файл — тест-файлы итерации 1 байт-неизменны
(NFR-003); негативные сценарии — `model_validate` с dict-payload
(строгий pyrefly, DESIGN-015).
"""

from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.decision import Decision
from disputatio.contracts.events import Event
from disputatio.contracts.proposal import ProposalFrontmatter
from disputatio.contracts.review import Review
from disputatio.contracts.session import BudgetUsed, Limits, SessionState
from disputatio.contracts.verification import VerificationReport


def proposal_payload(round_value: int) -> dict[str, Any]:
    """Минимальный валидный payload `proposal.md`-фронтматтера (§4.2)."""
    return {
        "schema": "disputatio/v1",
        "round": round_value,
        "role": "author",
        "responds_to": None,
        "files_touched": ["src/x.py"],
        "self_declared_status": "complete",
    }


def verification_payload(round_value: int) -> dict[str, Any]:
    """Минимальный валидный payload `verification.json` (§4.3)."""
    return {
        "schema": "disputatio/v1",
        "round": round_value,
        "gates": [],
        "overall": "pass",
        "diff_stats": {"files": 1, "insertions": 2, "deletions": 0},
    }


def review_payload(round_value: int) -> dict[str, Any]:
    """Минимальный валидный payload `review.json` (§4.4)."""
    return {
        "schema": "disputatio/v1",
        "round": round_value,
        "role": "reviewer",
        "verdict": "approve",
        "confidence": 0.9,
        "issues": [],
        "checked": ["src/x.py"],
        "summary": "изменения корректны",
    }


def decision_payload(round_value: int) -> dict[str, Any]:
    """Минимальный валидный payload `decision.json` (§4.5)."""
    return {
        "schema": "disputatio/v1",
        "round": round_value,
        "outcome": "continue",
        "reason": "review_request_changes",
        "open_issues_carried": [],
        "next_round_directive": "исправить замечания R-1",
    }


def round_artifact_cases(
    round_value: int,
) -> list[tuple[type[Any], dict[str, Any]]]:
    """Все четыре раундовых артефакта с полем `round` (DESIGN-016)."""
    return [
        (ProposalFrontmatter, proposal_payload(round_value)),
        (VerificationReport, verification_payload(round_value)),
        (Review, review_payload(round_value)),
        (Decision, decision_payload(round_value)),
    ]


def session_payload(current_round: int) -> dict[str, Any]:
    """Минимальный валидный payload `session.json` (§4.1)."""
    return {
        "schema": "disputatio/v1",
        "session_id": "0f9b7c2e-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
        "created_at": "2026-08-09T12:00:00+00:00",
        "state": "PROPOSING",
        "current_round": current_round,
        "task": {"prompt": "задача", "attachments": [], "mode": "develop"},
        "agents": {
            "author": {"adapter": "claude_code", "model": "m-author"},
            "reviewer": {"adapter": "codex", "model": "m-reviewer"},
        },
        "limits": {
            "max_rounds": 4,
            "max_total_tokens": 400000,
            "max_wall_seconds": 1800,
            "schema_retries": 2,
        },
        "budget_used": {"tokens": 0, "wall_seconds": 0.0, "cost_usd_est": 0.0},
    }


def limits_payload(max_rounds: int) -> dict[str, Any]:
    """Payload `Limits` (§4.1 `limits`) с параметризуемым `max_rounds`."""
    return {
        "max_rounds": max_rounds,
        "max_total_tokens": 400000,
        "max_wall_seconds": 1800,
        "schema_retries": 2,
    }


def event_payload(round_value: int | None) -> dict[str, Any]:
    """Минимальный валидный payload строки `events.jsonl` (§8)."""
    return {
        "schema": "disputatio/v1",
        "ts": "2026-08-09T12:00:00+00:00",
        "session": "0f9b7c2e-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
        "round": round_value,
        "source": "orchestrator",
        "type": "state_change",
        "payload": {},
    }


def test_negative_round_rejected_in_round_artifacts() -> None:
    """`round=-1` отклоняется схемой всех четырёх раундовых артефактов."""
    rejected: list[str] = []
    for model, payload in round_artifact_cases(-1):
        try:
            model.model_validate(payload)
        except ValidationError:
            rejected.append(model.__name__)
    assert rejected == [
        "ProposalFrontmatter",
        "VerificationReport",
        "Review",
        "Decision",
    ]


def test_zero_round_rejected_in_round_artifacts() -> None:
    """`round=0` отклоняется: раунды нумеруются с 1 (`rounds/001`)."""
    for model, payload in round_artifact_cases(0):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_round_one_accepted_in_round_artifacts() -> None:
    """Граничный `round=1` принимается всеми раундовыми артефактами."""
    for model, payload in round_artifact_cases(1):
        artifact = model.model_validate(payload)
        assert artifact.round == 1


def test_negative_current_round_rejected() -> None:
    """`current_round=-1` отклоняется: счётчик не бывает отрицательным."""
    with pytest.raises(ValidationError):
        SessionState.model_validate(session_payload(-1))


def test_current_round_zero_accepted() -> None:
    """Граничный `current_round=0` (до первого раунда) принимается."""
    state = SessionState.model_validate(session_payload(0))
    assert state.current_round == 0


def test_negative_budget_tokens_rejected() -> None:
    """`budget_used.tokens=-1` отклоняется: счётчик стартует с нуля."""
    with pytest.raises(ValidationError):
        BudgetUsed.model_validate({"tokens": -1})


def test_budget_tokens_zero_accepted() -> None:
    """Граничный `tokens=0` принимается (стартовое значение счётчика)."""
    budget = BudgetUsed.model_validate({"tokens": 0})
    assert budget.tokens == 0


def test_max_rounds_zero_rejected() -> None:
    """`max_rounds=0` отклоняется: лимит < 1 делает loop невозможным."""
    with pytest.raises(ValidationError):
        Limits.model_validate(limits_payload(0))


def test_max_rounds_one_accepted() -> None:
    """Граничный `max_rounds=1` принимается."""
    limits = Limits.model_validate(limits_payload(1))
    assert limits.max_rounds == 1


def test_event_zero_round_rejected() -> None:
    """`Event` с `round=0` отклоняется: вне-раундовые события несут `None`."""
    with pytest.raises(ValidationError):
        Event.model_validate(event_payload(0))


def test_event_none_round_accepted() -> None:
    """`Event` с `round=None` (событие вне раунда) принимается."""
    event = Event.model_validate(event_payload(None))
    assert event.round is None
