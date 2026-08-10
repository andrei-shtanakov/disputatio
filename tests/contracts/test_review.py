"""Тесты модели review.json: TASK-005, [DESIGN-005], [REQ-005].

Импорты `disputatio.contracts.review` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы
collection. Red-селектор (`test_spec_4_4_example_validates`) превращает
ImportError в AssertionError — гейт принимает red только при падении
assertion'ом.
"""

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError


def spec_4_4_example() -> dict[str, Any]:
    """Пример Review из SPEC-001 §4.4 (конкретизированы плейсхолдеры)."""
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
                "evidence": (
                    "чем подтверждено: цитата diff, вывод команды, "
                    "ссылка на verification gate"
                ),
                "suggestion": "как исправить (опционально)",
            }
        ],
        "checked": [
            "прочитал diff",
            "запустил git diff --stat",
            "прочитал tests/...",
        ],
        "summary": "1–3 предложения",
    }


def test_spec_4_4_example_validates() -> None:
    """Пример §4.4 валидируется дословно из фикстуры-словаря."""
    try:
        from disputatio.contracts.review import Review
    except ImportError as exc:  # red-фаза: review.py ещё не создан
        raise AssertionError(
            "src/disputatio/contracts/review.py ещё не создан"
        ) from exc

    review = Review.model_validate(spec_4_4_example())
    assert review.round == 3
    assert review.role == "reviewer"
    assert review.verdict == "request_changes"
    assert review.confidence == 0.8
    assert len(review.issues) == 1
    issue = review.issues[0]
    assert issue.id == "R3-1"
    assert issue.severity == "blocker"
    assert issue.file == "src/x.py"
    assert issue.line_hint == 42
    assert issue.suggestion == "как исправить (опционально)"
    assert review.checked[0] == "прочитал diff"
    assert review.summary == "1–3 предложения"


def test_round_trip_with_schema_key() -> None:
    """Round-trip через JSON: ключ `"schema"` присутствует, модель равна."""
    from disputatio.contracts.review import Review

    original = Review.model_validate(spec_4_4_example())
    dumped = original.model_dump_json(by_alias=True)
    assert json.loads(dumped)["schema"] == "disputatio/v1"
    assert Review.model_validate_json(dumped) == original


def test_all_valid_verdicts_accepted() -> None:
    """Каждый verdict из §4.4 (approve/request_changes/reject) принимается."""
    from disputatio.contracts.review import Review

    for valid in ("approve", "request_changes", "reject"):
        payload = copy.deepcopy(spec_4_4_example())
        payload["verdict"] = valid
        assert Review.model_validate(payload).verdict == valid


def test_invalid_verdict_rejected() -> None:
    """`verdict` вне approve/request_changes/reject отклоняется."""
    from disputatio.contracts.review import Review

    for invalid in ("ship_it", "approved", ""):
        payload = copy.deepcopy(spec_4_4_example())
        payload["verdict"] = invalid
        with pytest.raises(ValidationError):
            Review.model_validate(payload)


def test_invalid_severity_rejected() -> None:
    """`severity` вне blocker/major/minor/nit отклоняется."""
    from disputatio.contracts.review import Review

    for invalid in ("critical", "info", ""):
        payload = copy.deepcopy(spec_4_4_example())
        payload["issues"][0]["severity"] = invalid
        with pytest.raises(ValidationError):
            Review.model_validate(payload)


def test_confidence_outside_unit_interval_rejected() -> None:
    """`confidence` вне [0.0, 1.0] отклоняется; границы 0.0 и 1.0 валидны."""
    from disputatio.contracts.review import Review

    for invalid in (-0.1, 1.1):
        payload = copy.deepcopy(spec_4_4_example())
        payload["confidence"] = invalid
        with pytest.raises(ValidationError):
            Review.model_validate(payload)

    for boundary in (0.0, 1.0):
        payload = copy.deepcopy(spec_4_4_example())
        payload["confidence"] = boundary
        assert Review.model_validate(payload).confidence == boundary


def test_missing_checked_rejected_empty_checked_allowed() -> None:
    """Отсутствующий `checked` отклоняется; `checked == []` схемно проходит.

    Правило «пустой checked ⇒ ревью не принято» — кросс-артефактный слой
    (validation.py, REQ-011), не схема.
    """
    from disputatio.contracts.review import Review

    payload = copy.deepcopy(spec_4_4_example())
    del payload["checked"]
    with pytest.raises(ValidationError):
        Review.model_validate(payload)

    payload = copy.deepcopy(spec_4_4_example())
    payload["checked"] = []
    assert Review.model_validate(payload).checked == []


def test_role_only_reviewer() -> None:
    """`role` — Literal: только "reviewer", иная роль отклоняется."""
    from disputatio.contracts.review import Review

    for invalid in ("author", "orchestrator", ""):
        payload = copy.deepcopy(spec_4_4_example())
        payload["role"] = invalid
        with pytest.raises(ValidationError):
            Review.model_validate(payload)


def test_issue_optional_fields_default() -> None:
    """Без evidence/suggestion/line_hint: "" / None / None; issues → []."""
    from disputatio.contracts.review import Review

    payload = copy.deepcopy(spec_4_4_example())
    issue = payload["issues"][0]
    del issue["evidence"]
    del issue["suggestion"]
    del issue["line_hint"]
    parsed = Review.model_validate(payload).issues[0]
    assert parsed.evidence == ""
    assert parsed.suggestion is None
    assert parsed.line_hint is None

    payload = copy.deepcopy(spec_4_4_example())
    del payload["issues"]
    assert Review.model_validate(payload).issues == []
