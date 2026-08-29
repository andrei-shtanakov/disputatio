"""Тесты схемы `disputatio/v2` для doc-сессий: TASK-1, SPEC-002 §5.1, §5.2.

Импорты новых символов (`disputatio.contracts.checklist`, `Mode.DOCUMENT`,
`Review.checklist`, `Issue.defect_class`) выполняются внутри тестов: на
момент red-чекпоинта модуля `checklist.py` ещё нет, и импорт на уровне
модуля сломал бы collection. Red-селектор (`test_mode_document_exists`)
превращает ImportError в AssertionError — гейт принимает red только при
падении assertion'ом.
"""

import copy
from typing import Any

import pytest
from pydantic import ValidationError


def v1_session_payload() -> dict[str, Any]:
    """Валидный v1 SessionState (develop, без v2-полей)."""
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


def v1_review_payload() -> dict[str, Any]:
    """Валидный v1 Review (без checklist, без defect_class)."""
    return {
        "schema": "disputatio/v1",
        "round": 3,
        "role": "reviewer",
        "verdict": "request_changes",
        "confidence": 0.8,
        "issues": [
            {
                "id": "R3-1",
                "severity": "blocker",
                "file": "src/x.py",
                "line_hint": 42,
                "claim": "что не так, проверяемая формулировка",
                "evidence": "цитата diff",
                "suggestion": None,
            }
        ],
        "checked": ["прочитал diff"],
        "summary": "1-3 предложения",
    }


def checklist_item_payload(**overrides: Any) -> dict[str, Any]:
    """Валидный ChecklistItem-payload с одной evidence-ссылкой."""
    payload: dict[str, Any] = {
        "id": "S1",
        "status": "pass",
        "evidence": [{"kind": "artifact", "ref": "docs/specs/x.md", "lines": "34-41"}],
        "issue_ids": [],
    }
    payload.update(overrides)
    return payload


def test_mode_document_exists() -> None:
    """`Mode.DOCUMENT == "document"` — новый режим doc-сессий (§5.1)."""
    try:
        from disputatio.contracts.session import Mode
    except ImportError as exc:  # red-фаза
        raise AssertionError(
            "src/disputatio/contracts/session.py ещё не поддерживает Mode.DOCUMENT"
        ) from exc

    assert Mode.DOCUMENT == "document"


def test_v2_reader_accepts_v1_payload() -> None:
    """v2-совместимая модель принимает v1-payload без v2-полей."""
    from disputatio.contracts.session import SessionState

    state = SessionState.model_validate(v1_session_payload())
    assert state.task.mode == "develop"


def test_v1_strict_rejects_document_mode() -> None:
    """v1-тег + `task.mode == document` — ValidationError: режим только v2."""
    from disputatio.contracts.session import SessionState

    payload = copy.deepcopy(v1_session_payload())
    payload["task"]["mode"] = "document"
    with pytest.raises(ValidationError):
        SessionState.model_validate(payload)


def test_v1_rejects_checklist() -> None:
    """v1-тег + непустой `checklist` в Review — ValidationError."""
    from disputatio.contracts.review import Review

    payload = copy.deepcopy(v1_review_payload())
    payload["checklist"] = [checklist_item_payload()]
    with pytest.raises(ValidationError):
        Review.model_validate(payload)


def test_v1_rejects_defect_class() -> None:
    """v1-тег + `issues[].defect_class` в Review — ValidationError."""
    from disputatio.contracts.review import Review

    payload = copy.deepcopy(v1_review_payload())
    payload["issues"][0]["defect_class"] = "architectural"
    with pytest.raises(ValidationError):
        Review.model_validate(payload)


def test_v2_accepts_plain_v1_payload() -> None:
    """v2-тег без единого v2-поля валиден: v1-payload — подмножество v2."""
    from disputatio.contracts.review import Review

    payload = copy.deepcopy(v1_review_payload())
    payload["schema"] = "disputatio/v2"
    review = Review.model_validate(payload)
    assert review.checklist is None
    assert review.issues[0].defect_class is None


def test_checklist_item_requires_evidence() -> None:
    """Пустой `evidence` у ChecklistItem — ValidationError (min_length=1)."""
    from disputatio.contracts.checklist import ChecklistItem

    payload = checklist_item_payload(evidence=[])
    with pytest.raises(ValidationError):
        ChecklistItem.model_validate(payload)


def test_defect_class_optional_default_none() -> None:
    """`Issue` без `defect_class` — дефолт `None`."""
    from disputatio.contracts.review import Issue

    issue = Issue.model_validate(
        {
            "id": "R1-1",
            "severity": "minor",
            "file": "src/x.py",
            "claim": "заявление",
        }
    )
    assert issue.defect_class is None


def test_artifact_evidence_requires_lines() -> None:
    """`ArtifactEvidence` без `lines` — ValidationError: поле обязательно."""
    from disputatio.contracts.checklist import ArtifactEvidence

    with pytest.raises(ValidationError):
        ArtifactEvidence.model_validate({"kind": "artifact", "ref": "x.md"})


def test_gate_evidence_rejects_lines() -> None:
    """`GateEvidence` с `lines` — ValidationError: лишнее поле (extra=forbid)."""
    from disputatio.contracts.checklist import GateEvidence

    with pytest.raises(ValidationError):
        GateEvidence.model_validate(
            {"kind": "gate", "ref": "doc-links", "lines": "1-2"}
        )


@pytest.mark.parametrize("valid_lines", ["34-41", "12"])
def test_evidence_lines_format(valid_lines: str) -> None:
    """`lines` в формате `"N"` или `"N-M"` принимается."""
    from disputatio.contracts.checklist import ArtifactEvidence

    evidence = ArtifactEvidence.model_validate(
        {"kind": "artifact", "ref": "x.md", "lines": valid_lines}
    )
    assert evidence.lines == valid_lines


@pytest.mark.parametrize("invalid_lines", ["abc", "41-34"])
def test_evidence_lines_format_rejected(invalid_lines: str) -> None:
    """Неверный формат/порядок `lines` (`"abc"`, `"41-34"`) — ValidationError."""
    from disputatio.contracts.checklist import ArtifactEvidence

    with pytest.raises(ValidationError):
        ArtifactEvidence.model_validate(
            {"kind": "artifact", "ref": "x.md", "lines": invalid_lines}
        )
