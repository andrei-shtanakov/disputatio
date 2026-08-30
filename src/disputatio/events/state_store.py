"""Файловая реализация порта `StateStore` ([DESIGN-003], [REQ-004], [REQ-005]).

Один `artifact_root` — одна сессия ([ADR-006], редакция SPEC-002 §4.1):
состояние всегда лежит в `artifact_root/.disputatio/session.json`, а
`session_id` в путь не входит. Поэтому `load(session_id)` использует аргумент
только как ключ проверки: не тот `session_id` — `KeyError`, ровно как
отсутствующий файл.

Прежняя редакция говорила «одна рабочая директория — одна сессия» и связывала
состояние с рабочим git-репозиторием. Формулировка сменилась не ради слов:
пайплайн размещает несколько сессий под ОДНИМ репозиторием
(`pipelines/<slug>/sessions/<revision>/`), и правило про рабочую директорию
запрещало бы ровно это. Один журнал по-прежнему держит одну сессию — просто
журнал больше не обязан совпадать с репозиторием.
"""

import json
from pathlib import Path

from disputatio.contracts.session import SessionState
from disputatio.events.atomic import atomic_write
from disputatio.events.paths import session_json_path


class FileStateStore:
    """`ports.StateStore`: `session.json` под `artifact_root/.disputatio/`.

    Корень — журнал сессии, а не рабочий репозиторий (SPEC-002 §4.1): две
    сессии над одним git-деревом различаются именно им, и общий корень свёл бы
    их в один `session.json`.

    Предусловие `save`: `bootstrap_session(artifact_root)` уже вызван —
    `atomic_write` кладёт временный файл рядом с целевым, поэтому без
    `.disputatio/` вызов упадёт `FileNotFoundError` (bootstrap — единственная
    точка `mkdir`, [REQ-002]).
    """

    def __init__(self, artifact_root: Path) -> None:
        self._path = session_json_path(artifact_root)

    def load(self, session_id: str) -> SessionState:
        """Читает `session.json` и возвращает состояние сессии `session_id`.

        `KeyError(session_id)`, если файла нет или `session_id` в файле не
        совпадает с запрошенным ([ADR-006]). Схемно невалидный payload —
        `pydantic.ValidationError` наружу без перехвата ([REQ-005]):
        повреждённое состояние не маскируется под «сессии нет».
        """
        try:
            payload = self._path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise KeyError(session_id) from exc

        state = SessionState.model_validate(json.loads(payload))
        if state.session_id != session_id:
            raise KeyError(session_id)
        return state

    def save(self, state: SessionState) -> None:
        """Атомарно перезаписывает `session.json` целиком ([REQ-004], [REQ-001]).

        Сериализация — публичным `model_dump(mode="json", by_alias=True)`, без
        ручной сборки полей: схема остаётся собственностью `disputatio.contracts`
        ([REQ-011]). Файл пишется целиком, поэтому смешения полей соседних
        сохранений не бывает.
        """
        payload = json.dumps(
            state.model_dump(mode="json", by_alias=True), ensure_ascii=False
        )
        atomic_write(self._path, payload)
