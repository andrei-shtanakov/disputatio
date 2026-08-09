"""Нативная строка → `Event` через `translate_line` — TASK-006.

[DESIGN-005], [REQ-008], [REQ-009]. Оба теста изолируют `translate_line` от
адаптеров: `parser` — фейковая чистая функция, а не `_parse_native_line`
конкретного CLI, так что правило «нераспознанное → `agent_text_delta` с
`raw: true`» проверяется ровно там, где живёт (§8 SPEC-001).
"""

from datetime import UTC, datetime

from disputatio.contracts.events import EventSource, EventType

FIXED_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

_NOT_IMPLEMENTED = "events.translate_line ещё не реализован из TASK-006"


def test_recognized_line_maps_to_typed_event() -> None:
    try:
        from disputatio.adapters.events import translate_line
    except ImportError as exc:
        raise AssertionError(_NOT_IMPLEMENTED) from exc

    def _parser(line: str) -> tuple[EventType, dict[str, object]] | None:
        if line == "TOOL Read":
            return EventType.AGENT_TOOL_USE, {"tool": "Read"}
        return None

    event = translate_line(
        "TOOL Read",
        parser=_parser,
        session="s-1",
        source=EventSource.REVIEWER,
        round_no=3,
        ts=FIXED_TS,
    )

    assert event.type is EventType.AGENT_TOOL_USE
    assert event.payload == {"tool": "Read"}
    assert event.source is EventSource.REVIEWER
    assert event.session == "s-1"
    assert event.round == 3
    assert event.ts == FIXED_TS


def test_unrecognized_line_falls_back_to_raw_delta() -> None:
    try:
        from disputatio.adapters.events import translate_line
    except ImportError as exc:
        raise AssertionError(_NOT_IMPLEMENTED) from exc

    raw_line = '{"type": "totally_unknown"} и свободный текст'

    event = translate_line(
        raw_line,
        parser=lambda _line: None,
        session="s-1",
        source=EventSource.AUTHOR,
        round_no=None,
        ts=FIXED_TS,
    )

    assert event.type is EventType.AGENT_TEXT_DELTA
    assert event.payload["raw"] is True
    assert event.payload["text"] == raw_line
    assert event.source is EventSource.AUTHOR
    assert event.round is None
