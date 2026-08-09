"""Файловая реализация порта `EventSink` ([DESIGN-004], [REQ-006], [REQ-007]).

Единственная пишущая операция `disputatio.events`, которая НЕ идёт через
`atomic_write`: журнал дописывается, а не перезаписывается целиком, поэтому
temp-file + rename здесь неприменим ([ADR-003]).
"""

import os
from pathlib import Path

from disputatio.contracts.events import Event
from disputatio.events.paths import events_jsonl_path


class JsonlEventSink:
    """Файловая реализация `ports.EventSink`: append-only `events.jsonl`.

    Предусловие `emit`: `bootstrap_session(root)` уже вызван — директорию
    `.disputatio/` sink не создаёт (bootstrap — единственная точка `mkdir`,
    [REQ-002]).
    """

    def __init__(self, root: Path) -> None:
        self._path = events_jsonl_path(root)

    def emit(self, event: Event) -> None:
        """Дописывает одну JSON-строку + `"\\n"` в конец `events.jsonl`.

        Открытие в `"ab"` (`O_APPEND`) — запись всегда идёт в текущий конец
        файла, предыдущие байты этим вызовом физически не перезаписываются:
        первые N строк остаются побайтово прежними ([REQ-006]). Строка
        собирается целиком в памяти и уходит одним `write` — параллельные
        `emit` не расщепляют строки друг друга, а порядок строк равен порядку
        вызовов ([REQ-006]).

        Файл открывается и закрывается на каждый вызов: между вызовами не
        остаётся дескриптора, который пришлось бы закрывать при восстановлении
        после kill'а ([DESIGN-004]). `fsync` перед закрытием — строка на диске
        к моменту возврата.

        Хвост файла перед записью не читается и не чинится: если предыдущий
        процесс умер на частичной строке, `emit` не бросает, но новая строка
        слипается с мусором — репарация принадлежит `w-runtime` ([ADR-003],
        [REQ-007]).
        """
        line = event.model_dump_json(by_alias=True) + "\n"
        with self._path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
