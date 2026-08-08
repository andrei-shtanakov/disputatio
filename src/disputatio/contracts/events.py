"""Модель строки `events.jsonl` — Event, EventSource, EventType.

[DESIGN-007], [REQ-007].

Схема §8 SPEC-001: UI — чистый подписчик events.jsonl, ядро про UI не знает.
`payload` — свободный dict: типизация payload по типам событий — расширение
позже, не v1 (правило адаптера §8 «всё нераспознанное → `agent_text_delta`
с `raw: true`» живёт в payload).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from disputatio.contracts.base import ArtifactBase


class EventSource(StrEnum):
    """Источник события §8; сериализуется строкой (JSON-совместимо)."""

    AUTHOR = "author"
    REVIEWER = "reviewer"
    VERIFIER = "verifier"
    ORCHESTRATOR = "orchestrator"


class EventType(StrEnum):
    """Тип события §8 — единственный словарь, который знает UI."""

    STATE_CHANGE = "state_change"
    AGENT_TEXT_DELTA = "agent_text_delta"
    AGENT_TOOL_USE = "agent_tool_use"
    GATE_STARTED = "gate_started"
    GATE_FINISHED = "gate_finished"
    ARTIFACT_WRITTEN = "artifact_written"
    ERROR = "error"


class Event(ArtifactBase):
    """Одна строка append-only лога `events.jsonl` (§8 SPEC-001).

    `round` nullable с default `None`: события вне раунда (создание
    сессии) раунда не имеют. `payload` — произвольный вложенный dict;
    default — пустой dict.
    """

    ts: datetime
    session: str
    round: int | None = Field(default=None, ge=1)
    source: EventSource
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
