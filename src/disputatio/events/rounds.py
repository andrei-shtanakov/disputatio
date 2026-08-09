"""Артефакты раунда и иммутабельность I3 ([DESIGN-005], [REQ-008], [REQ-009]).

Модуль пишет `rounds/NNN/*` уже готовым текстом и ничего не знает о схемах
артефактов: `proposal.md`/`changes.patch` приходят свободным markdown/diff'ом
([REQ-012]), `verification.json`/`review.json`/`decision.json` — JSON-строкой
от `model_dump_json(by_alias=True)` вызывающей стороны. Так `disputatio.events`
остаётся несвязанным с формами конкретных артефактов раунда.

Факт финализации раунда хранится маркер-файлом `rounds/NNN/.finalized`, а не
центральным реестром ([ADR-004]): директория раунда самодостаточна, проверка —
один `Path.exists()`.
"""

from pathlib import Path

from disputatio.events.atomic import atomic_write
from disputatio.events.paths import round_dir

FINALIZED_MARKER_NAME = ".finalized"


class RoundImmutableError(Exception):
    """Попытка перезаписать артефакт финализированного раунда (I3, [REQ-009])."""


def write_round_artifact(root: Path, round_no: int, name: str, content: str) -> Path:
    """Атомарно пишет `rounds/{round_no:03d}/{name}` и возвращает путь к нему.

    Директория раунда создаётся при необходимости — раунд `NNN` появляется на
    диске вместе с первым своим артефактом ([REQ-008]). Отсутствие любого из
    артефактов (например, `changes.patch` в analyze-раунде) для этого модуля не
    ошибка: он пишет только то, о чём его попросили, и ничего не требует.

    `content` уходит на диск побайтово в UTF-8, без повторной сериализации и
    escape ([REQ-012]).

    `RoundImmutableError`, если раунд уже помечен `finalize_round`: проверка
    маркера идёт до создания временного файла, поэтому директория раунда
    остаётся нетронутой ([REQ-009]).
    """
    directory = round_dir(root, round_no)
    if (directory / FINALIZED_MARKER_NAME).exists():
        raise RoundImmutableError(
            f"раунд {round_no:03d} финализирован: {name} не перезаписывается"
        )

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    atomic_write(path, content)
    return path


def finalize_round(root: Path, round_no: int) -> None:
    """Помечает раунд неизменяемым — атомарно пишет пустой маркер `.finalized`.

    Идемпотентна: повторный вызов перезаписывает маркер тем же пустым
    содержимым и не бросает ([REQ-009]). Директория раунда создаётся при
    необходимости — финализировать можно и раунд, не оставивший артефактов.
    """
    directory = round_dir(root, round_no)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / FINALIZED_MARKER_NAME, "")
