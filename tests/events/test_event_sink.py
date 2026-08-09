"""Тесты `event_sink.py`: TASK-005, [DESIGN-004], [REQ-006], [REQ-007], [ADR-003].

Импорт `disputatio.events.event_sink` внутри тестов: на момент red-чекпоинта
модуля ещё нет, и импорт на уровне модуля сломал бы collection. Red-селектор
(`test_emit_appends_single_line`) превращает `ImportError` в `AssertionError` —
гейт принимает red только при падении assertion'ом.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from disputatio.contracts import ports
from disputatio.contracts.events import Event, EventSource, EventType
from disputatio.events.bootstrap import bootstrap_session
from disputatio.events.paths import events_jsonl_path


def make_event(
    *,
    minute: int = 0,
    session: str = "sess-1",
    round_no: int | None = 1,
    source: EventSource = EventSource.ORCHESTRATOR,
    type_: EventType = EventType.STATE_CHANGE,
    payload: dict[str, object] | None = None,
) -> Event:
    """Валидное событие §8; `minute` разводит события по `ts`."""
    return Event(
        ts=datetime(2026, 8, 9, 12, minute, tzinfo=UTC),
        session=session,
        round=round_no,
        source=source,
        type=type_,
        payload={"n": minute} if payload is None else payload,
    )


def test_emit_appends_single_line(session_root: Path) -> None:
    try:
        from disputatio.events.event_sink import JsonlEventSink
    except ImportError as exc:  # red-фаза: event_sink.py ещё не создан
        raise AssertionError(
            "src/disputatio/events/event_sink.py ещё не создан"
        ) from exc

    bootstrap_session(session_root)
    event = make_event()

    JsonlEventSink(session_root).emit(event)

    raw = events_jsonl_path(session_root).read_bytes()
    assert raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == event.model_dump(mode="json", by_alias=True)


def test_emit_preserves_prior_lines_byte_for_byte(session_root: Path) -> None:
    """REQ-006: первые N строк побайтово не изменены после N+1-го emit."""
    from disputatio.events.event_sink import JsonlEventSink

    bootstrap_session(session_root)
    sink = JsonlEventSink(session_root)
    path = events_jsonl_path(session_root)
    for minute in range(3):
        sink.emit(make_event(minute=minute))
    prefix = b"\n".join(path.read_bytes().split(b"\n")[:2]) + b"\n"

    sink.emit(make_event(minute=3))

    raw = path.read_bytes()
    assert raw.startswith(prefix)
    assert len(raw.decode("utf-8").splitlines()) == 4


def test_emit_order_matches_call_order(session_root: Path) -> None:
    """REQ-006: порядок строк совпадает с порядком вызовов emit."""
    from disputatio.events.event_sink import JsonlEventSink

    bootstrap_session(session_root)
    sink = JsonlEventSink(session_root)
    events = [make_event(minute=minute) for minute in range(5)]

    for event in events:
        sink.emit(event)

    lines = events_jsonl_path(session_root).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        event.model_dump(mode="json", by_alias=True) for event in events
    ]


def test_emit_after_partial_line_does_not_raise_and_appends(
    session_root: Path,
) -> None:
    """REQ-007/ADR-003: склейка с мусорным хвостом — известный, не крашащий эффект."""
    from disputatio.events.event_sink import JsonlEventSink

    bootstrap_session(session_root)
    sink = JsonlEventSink(session_root)
    path = events_jsonl_path(session_root)
    sink.emit(make_event(minute=0))
    complete = path.read_bytes()
    partial = b'{"schema": "disputatio/v1", "ts": "2026-08-09T1'
    with path.open("ab") as handle:
        handle.write(partial)

    event = make_event(minute=1)
    sink.emit(event)

    raw = path.read_bytes()
    assert raw.startswith(complete + partial)
    expected = json.dumps(
        event.model_dump(mode="json", by_alias=True), ensure_ascii=False
    )
    assert json.loads(raw[len(complete + partial) : -1].decode("utf-8")) == json.loads(
        expected
    )
    assert raw.endswith(b"\n")


def test_jsonl_event_sink_satisfies_event_sink_protocol(session_root: Path) -> None:
    from disputatio.events.event_sink import JsonlEventSink

    sink: ports.EventSink = JsonlEventSink(session_root)

    assert isinstance(sink, ports.EventSink)
