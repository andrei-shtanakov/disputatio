"""Журнал событий пайплайна: словарь §4.1, sink и парный читатель (P8).

Гарантии этого журнала намеренно слабее манифеста. Атомарно обновить оба
файла одной операцией нельзя, поэтому источник истины ровно один — манифест
(строго без дубликатов), а `events.jsonl` — производный **best-effort**
диагностический поток (zero-or-more): событие может отсутствовать (падение
между записью манифеста и события; resume пропущенные события не
регенерирует) и может дублироваться (повтор действия по тому же
`operation_id`).

Отсюда два следствия, оба реализованы здесь, а не у потребителя.
**Дедупликация поставляется вместе с журналом** (P8): `read_pipeline_events`
— штатный читатель, парный к sink'у; оставить подавление дублей
гипотетическому потребителю значило бы не дать гарантию, а переназначить её.
**Оборванный хвост чинится при открытии**: kill во время `emit` оставляет
частичную последнюю строку, и следующая запись слиплась бы с мусором.

Модели журнала живут здесь, а не в `contracts`: §9 SPEC-002 отдаёт этому
пакету файловые append-only writer'ы, а `PipelineEvent` вне своего файла
смысла не имеет — в отличие от артефактов пайплайна, которые читают и
пишут разные пакеты.
"""

import json
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from disputatio.events.pipeline_paths import events_path


class PipelineEventType(StrEnum):
    """Закрытый словарь событий уровня пайплайна (§4.1) — ровно шесть значений.

    События сессий остаются в их собственных `events.jsonl`; пайплайновый лог
    их не дублирует и несёт только своё.
    """

    PHASE_CHANGE = "phase_change"
    SESSION_STARTED = "session_started"
    SESSION_FINISHED = "session_finished"
    RETURN_RECORDED = "return_recorded"
    EXPORTED = "exported"
    ERROR = "error"


class PipelineEvent(BaseModel):
    """Одна строка `pipelines/<slug>/events.jsonl` (§4.1).

    `operation_id` в payload обязателен: он — единственный ключ, по которому
    читатель отличает повтор действия от нового (P8). Событие без него
    дедупликации не поддаётся, и записать его — значит молча испортить ленту.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    pipeline: str
    type: PipelineEventType
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_operation_id(self) -> "PipelineEvent":
        operation_id = self.payload.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError(
                "payload обязан нести непустой строковый operation_id — "
                "ключ дедупликации журнала (P8)"
            )
        return self

    @property
    def dedup_key(self) -> tuple[str, PipelineEventType]:
        """Ключ подавления дублей: `(operation_id, type)` (P8)."""
        return (str(self.payload["operation_id"]), self.type)


def _repair_tail(path: Path) -> None:
    """Усекает незавершённую последнюю строку журнала (P8).

    Порча бывает ровно двух видов, и обе — только в хвосте: строка без
    завершающего `\\n` (процесс умер посреди записи) либо целая строка с
    невалидным JSON. Обе усекаются до последней заведомо целой строки.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return
    if not data:
        return

    if not data.endswith(b"\n"):
        cut = data.rfind(b"\n") + 1
    else:
        start = data.rfind(b"\n", 0, len(data) - 1) + 1
        try:
            json.loads(data[start : len(data) - 1])
        except ValueError:
            cut = start
        else:
            return

    with path.open("r+b") as handle:
        handle.truncate(cut)
        os.fsync(handle.fileno())


class PipelineEventSink:
    """Append-only журнал событий пайплайна; best-effort по контракту P8.

    Ремонт хвоста делается один раз, при открытии: держать его в `emit`
    значило бы перечитывать весь файл на каждое событие ради случая, который
    возможен только после краха предыдущего процесса.
    """

    def __init__(self, workspace_root: Path, slug: str) -> None:
        self._path = events_path(workspace_root, slug)
        _repair_tail(self._path)

    @property
    def path(self) -> Path:
        """Путь журнала — вход для парного `read_pipeline_events`."""
        return self._path

    def emit(self, event: PipelineEvent) -> None:
        """Дописывает одну JSON-строку + `"\\n"` в конец журнала.

        Тип сверяется по словарю §4.1 отдельно от валидации модели: событие
        могло прийти из `model_construct` (обход валидации), и словарь обязан
        оставаться закрытым на границе файла, а не только на границе типа.

        Открытие в `"ab"` (`O_APPEND`) — прежние байты этим вызовом физически
        не перезаписываются; строка собирается целиком в памяти и уходит одним
        `write`, поэтому параллельные `emit` не расщепляют строки друг друга.
        """
        PipelineEventType(event.type)
        line = event.model_dump_json() + "\n"
        with self._path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())


def read_pipeline_events(path: Path) -> list[PipelineEvent]:
    """Читает журнал, подавляя дубли по `(operation_id, type)` (P8).

    Штатный читатель, парный к sink'у: дедупликация — гарантия журнала, а не
    задача потребителя. Оборванный хвост и нечитаемые строки пропускаются
    молча — журнал диагностический, и падать на нём означало бы дать
    производному потоку право уронить того, кто его читает. Отсутствующий
    файл — пустой список по той же причине (zero-or-more).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return []

    events: list[PipelineEvent] = []
    seen: set[tuple[str, PipelineEventType]] = set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = PipelineEvent.model_validate(json.loads(line))
        except ValueError:
            continue
        if event.dedup_key in seen:
            continue
        seen.add(event.dedup_key)
        events.append(event)
    return events
