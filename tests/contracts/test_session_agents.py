"""Тесты TASK-019: обязательные ключи author и reviewer в agents.

[REQ-019], [DESIGN-017]: `SessionState.agents: dict[Role, AgentRef]` схемно
допускает отсутствие любого из агентов; полнота — after-валидатор
`_require_both_agents`, требующий оба ключа `Role`. Тип поля не меняется
(доступ `agents[Role.AUTHOR]` в существующих тестах сохраняется), JSON-формат
§4.1 не меняется. Новый файл — тест-файлы предыдущих итераций байт-неизменны
(NFR-003); негативные сценарии — `model_validate` с dict-payload (строгий
pyrefly, DESIGN-015).
"""

from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.base import Role
from disputatio.contracts.session import SessionState


def both_agents() -> dict[str, Any]:
    """Полный блок `agents` §4.1: оба ключа author и reviewer."""
    return {
        "author": {"adapter": "claude_code", "model": "m-author"},
        "reviewer": {"adapter": "codex", "model": "m-reviewer"},
    }


def session_payload(agents: dict[str, Any]) -> dict[str, Any]:
    """Валидный payload `session.json` (§4.1) с параметризуемым `agents`."""
    return {
        "schema": "disputatio/v1",
        "session_id": "0f9b7c2e-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
        "created_at": "2026-08-09T12:00:00+00:00",
        "state": "PROPOSING",
        "current_round": 0,
        "task": {"prompt": "задача", "attachments": [], "mode": "develop"},
        "agents": agents,
        "limits": {
            "max_rounds": 4,
            "max_total_tokens": 400000,
            "max_wall_seconds": 1800,
            "schema_retries": 2,
        },
        "budget_used": {"tokens": 0, "wall_seconds": 0.0, "cost_usd_est": 0.0},
    }


def test_missing_reviewer_rejected() -> None:
    """Payload без ключа `reviewer` отклоняется ValidationError."""
    agents = both_agents()
    del agents["reviewer"]
    with pytest.raises(ValidationError):
        SessionState.model_validate(session_payload(agents))


def test_missing_author_rejected() -> None:
    """Payload без ключа `author` отклоняется ValidationError."""
    agents = both_agents()
    del agents["author"]
    with pytest.raises(ValidationError):
        SessionState.model_validate(session_payload(agents))


def test_empty_agents_rejected() -> None:
    """`agents == {}` отклоняется: debate loop требует обоих агентов."""
    with pytest.raises(ValidationError):
        SessionState.model_validate(session_payload({}))


def test_both_agents_accepted() -> None:
    """Payload с обоими ключами принимается; доступ по `Role` сохраняется."""
    state = SessionState.model_validate(session_payload(both_agents()))
    assert set(state.agents) == {Role.AUTHOR, Role.REVIEWER}
    assert state.agents[Role.AUTHOR].adapter == "claude_code"


def test_error_message_names_missing_role() -> None:
    """Сообщение об ошибке называет недостающую роль (`reviewer`)."""
    agents = both_agents()
    del agents["reviewer"]
    try:
        SessionState.model_validate(session_payload(agents))
    except ValidationError as exc:
        assert "reviewer" in str(exc)
    else:
        raise AssertionError("payload без reviewer прошёл валидацию")
