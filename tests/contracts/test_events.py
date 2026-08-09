"""Тесты модели события events.jsonl: TASK-007, [DESIGN-007], [REQ-007].

Импорты `disputatio.contracts.events` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор (`test_spec_8_json_line_validates`) превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
"""

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError


def spec_8_example() -> dict[str, Any]:
    """Пример события из SPEC-001 §8 (конкретизированы плейсхолдеры)."""
    return {
        "schema": "disputatio/v1",
        "ts": "2026-08-08T12:00:00+00:00",
        "session": "sess-20260808-01",
        "round": 3,
        "source": "author",
        "type": "state_change",
        "payload": {},
    }


def test_spec_8_json_line_validates() -> None:
    """JSON-строка события §8 (строка events.jsonl) валидируется дословно."""
    try:
        from disputatio.contracts.events import Event
    except ImportError as exc:  # red-фаза: events.py ещё не создан
        raise AssertionError(
            "src/disputatio/contracts/events.py ещё не создан"
        ) from exc

    event = Event.model_validate_json(json.dumps(spec_8_example()))
    assert event.ts.isoformat() == "2026-08-08T12:00:00+00:00"
    assert event.session == "sess-20260808-01"
    assert event.round == 3
    assert event.source == "author"
    assert event.type == "state_change"
    assert event.payload == {}


def test_round_none_allowed_and_is_default() -> None:
    """`round=None` допустим (событие вне раунда), он же — default."""
    from disputatio.contracts.events import Event

    explicit = copy.deepcopy(spec_8_example())
    explicit["round"] = None
    assert Event.model_validate(explicit).round is None

    omitted = copy.deepcopy(spec_8_example())
    del omitted["round"]
    assert Event.model_validate(omitted).round is None


def test_all_valid_sources_and_types_accepted() -> None:
    """Каждая пара source×type из §8 принимается схемой."""
    from disputatio.contracts.events import Event

    sources = ("author", "reviewer", "verifier", "orchestrator")
    types = (
        "state_change",
        "agent_text_delta",
        "agent_tool_use",
        "gate_started",
        "gate_finished",
        "artifact_written",
        "error",
    )
    for source in sources:
        for type_ in types:
            payload = copy.deepcopy(spec_8_example())
            payload["source"] = source
            payload["type"] = type_
            event = Event.model_validate(payload)
            assert event.source == source
            assert event.type == type_


def test_invalid_source_rejected() -> None:
    """`source` вне четвёрки §8 отклоняется."""
    from disputatio.contracts.events import Event

    for invalid in ("user", "arbiter", "Author", ""):
        payload = copy.deepcopy(spec_8_example())
        payload["source"] = invalid
        with pytest.raises(ValidationError):
            Event.model_validate(payload)


def test_invalid_type_rejected() -> None:
    """`type` вне семёрки §8 отклоняется."""
    from disputatio.contracts.events import Event

    for invalid in ("state_changed", "tool_use", "warning", ""):
        payload = copy.deepcopy(spec_8_example())
        payload["type"] = invalid
        with pytest.raises(ValidationError):
            Event.model_validate(payload)


def test_round_trip_with_schema_key() -> None:
    """Round-trip через JSON: ключ `"schema"` присутствует, модель равна."""
    from disputatio.contracts.events import Event

    original = Event.model_validate(spec_8_example())
    dumped = original.model_dump_json(by_alias=True)
    assert json.loads(dumped)["schema"] == "disputatio/v1"
    assert Event.model_validate_json(dumped) == original


def test_payload_defaults_to_empty_dict() -> None:
    """Без `payload` поле по умолчанию — пустой dict."""
    from disputatio.contracts.events import Event

    payload = copy.deepcopy(spec_8_example())
    del payload["payload"]
    assert Event.model_validate(payload).payload == {}


def test_arbitrary_nested_payload_passes() -> None:
    """Произвольный вложенный dict в `payload` (включая `raw: true`) проходит.

    Правило адаптера §8: всё нераспознанное → `agent_text_delta` с
    `raw: true` в payload; типизация payload по типам событий — не v1.
    """
    from disputatio.contracts.events import Event

    nested = {
        "raw": True,
        "text": "нераспознанная строка нативного потока",
        "meta": {"chunk": 7, "tags": ["stream-json", "fallback"]},
    }
    payload = copy.deepcopy(spec_8_example())
    payload["type"] = "agent_text_delta"
    payload["payload"] = nested
    event = Event.model_validate(payload)
    assert event.payload == nested
    assert event.payload["raw"] is True
