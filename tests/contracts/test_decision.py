"""Тесты модели decision.json: TASK-006, [DESIGN-006], [REQ-006].

Импорты `disputatio.contracts.decision` выполняются внутри тестов: на
момент red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы
collection. Red-селектор (`test_spec_4_5_example_validates`) превращает
ImportError в AssertionError — гейт принимает red только при падении
assertion'ом.
"""

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError


def spec_4_5_example() -> dict[str, Any]:
    """Пример Decision из SPEC-001 §4.5 (конкретизированы плейсхолдеры)."""
    return {
        "schema": "disputatio/v1",
        "round": 3,
        "outcome": "continue",
        "reason": "review_request_changes",
        "open_issues_carried": ["R3-2"],
        "next_round_directive": (
            "текст, который войдёт в prompt автора раунда 4 (null если terminal)"
        ),
    }


def test_spec_4_5_example_validates() -> None:
    """Пример §4.5 валидируется дословно из фикстуры-словаря."""
    try:
        from disputatio.contracts.decision import Decision
    except ImportError as exc:  # red-фаза: decision.py ещё не создан
        raise AssertionError(
            "src/disputatio/contracts/decision.py ещё не создан"
        ) from exc

    decision = Decision.model_validate(spec_4_5_example())
    assert decision.round == 3
    assert decision.outcome == "continue"
    assert decision.reason == "review_request_changes"
    assert decision.open_issues_carried == ["R3-2"]
    assert decision.next_round_directive == (
        "текст, который войдёт в prompt автора раунда 4 (null если terminal)"
    )


def test_round_trip_with_schema_key() -> None:
    """Round-trip через JSON: ключ `"schema"` присутствует, модель равна."""
    from disputatio.contracts.decision import Decision

    original = Decision.model_validate(spec_4_5_example())
    dumped = original.model_dump_json(by_alias=True)
    assert json.loads(dumped)["schema"] == "disputatio/v1"
    assert Decision.model_validate_json(dumped) == original


def test_all_valid_outcomes_accepted() -> None:
    """Каждый outcome из §4.5 принимается схемой."""
    from disputatio.contracts.decision import Decision

    for valid in ("converged", "continue", "deadlock", "budget_hit", "failed"):
        payload = copy.deepcopy(spec_4_5_example())
        payload["outcome"] = valid
        assert Decision.model_validate(payload).outcome == valid


def test_invalid_outcome_rejected() -> None:
    """`outcome` вне пятёрки §4.5 отклоняется."""
    from disputatio.contracts.decision import Decision

    for invalid in ("done", "escalated", "approve", ""):
        payload = copy.deepcopy(spec_4_5_example())
        payload["outcome"] = invalid
        with pytest.raises(ValidationError):
            Decision.model_validate(payload)


def test_none_directive_allowed_for_terminal_outcomes() -> None:
    """`next_round_directive=None` допустим при terminal-исходе (§4.5)."""
    from disputatio.contracts.decision import Decision

    for terminal in ("converged", "deadlock", "budget_hit", "failed"):
        payload = copy.deepcopy(spec_4_5_example())
        payload["outcome"] = terminal
        payload["next_round_directive"] = None
        decision = Decision.model_validate(payload)
        assert decision.outcome == terminal
        assert decision.next_round_directive is None


def test_continue_with_none_directive_schema_valid() -> None:
    """Бэклог-пункт: `continue` + `next_round_directive=None` схемно валиден.

    Правило «continue обязан нести директиву» — если оно вообще нужно —
    кросс-артефактный слой, не схема: схемная валидация (ретрай агента с
    текстом ошибки pydantic) и протокольная валидация не смешиваются.
    """
    from disputatio.contracts.decision import Decision

    payload = copy.deepcopy(spec_4_5_example())
    payload["outcome"] = "continue"
    payload["next_round_directive"] = None
    decision = Decision.model_validate(payload)
    assert decision.outcome == "continue"
    assert decision.next_round_directive is None


def test_open_issues_carried_defaults_to_empty() -> None:
    """Без `open_issues_carried` поле по умолчанию — пустой список."""
    from disputatio.contracts.decision import Decision

    payload = copy.deepcopy(spec_4_5_example())
    del payload["open_issues_carried"]
    assert Decision.model_validate(payload).open_issues_carried == []
