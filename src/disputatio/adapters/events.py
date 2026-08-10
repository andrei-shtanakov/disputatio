"""Нативная строка CLI → `Event` ([DESIGN-005], TASK-006).

Правило §8 SPEC-001 «всё нераспознанное → `agent_text_delta` с `raw: true`»
живёт здесь ровно один раз: каждый адаптер приносит свой
`NativeLineParser`, а fallback общий, поэтому потеря текста невозможна ни
для одного CLI (REQ-009).

Модуль чистый: ни часов, ни нумерации раундов — `ts`/`session`/`round_no`
передаёт вызывающий `run()`.
"""

from collections.abc import Callable
from datetime import datetime

from disputatio.contracts.events import Event, EventSource, EventType

NativeLineParser = Callable[[str], tuple[EventType, dict[str, object]] | None]
"""Парсер строки конкретного CLI: `(EventType, payload)` или `None`."""


def translate_line(
    raw_line: str,
    *,
    parser: NativeLineParser,
    session: str,
    source: EventSource,
    round_no: int | None,
    ts: datetime,
) -> Event:
    """Переводит одну строку `stdout` агента в `Event` журнала §8.

    `parser` вернул `None` (строка не распознана) → `agent_text_delta` с
    `raw: true` и полным исходным текстом в `payload["text"]`.
    """
    parsed = parser(raw_line)
    if parsed is None:
        event_type = EventType.AGENT_TEXT_DELTA
        payload: dict[str, object] = {"raw": True, "text": raw_line}
    else:
        event_type, payload = parsed
    return Event(
        ts=ts,
        session=session,
        round=round_no,
        source=source,
        type=event_type,
        payload=payload,
    )
