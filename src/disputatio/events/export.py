"""Экспорт результата сессии: `result/*` + `manifest.json` ([DESIGN-006], [REQ-010]).

Модуль ничего не решает о содержании результата. `converged`, `round`,
`open_issues` приходят готовыми от вызывающей стороны произвольным `dict`
(а не pydantic-моделью — [ADR-005]) и уходят в `manifest.json` как есть;
`write_result` добавляет единственный вычисляемый ключ `"files"` с `sha256`
каждого экспортированного файла.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from disputatio.events.atomic import atomic_write
from disputatio.events.paths import result_dir

MANIFEST_NAME = "manifest.json"


def write_result(
    artifact_root: Path,
    files: Mapping[str, str],
    manifest: Mapping[str, Any],
) -> None:
    """Атомарно пишет `result/{name}` для каждого `files[name]`, затем манифест.

    Итоговый `result/manifest.json` = `manifest ∪ {"files": {name: {"sha256":
    ...}}}`. Ключ `"files"` вычисляется здесь и перекрывает одноимённое поле
    вызывающей стороны: checksum обязан описывать то, что реально легло на
    диск, иначе манифест перестаёт быть честным ([REQ-010]).

    Каждый файл, включая манифест, пишется через `atomic_write` — temp-file +
    rename ([REQ-001]). Атомарность здесь пофайловая, а не транзакционная на
    весь экспорт, и разница закрывается порядком: прежний `manifest.json`
    удаляется ДО первой перезаписи, а новый пишется последним. Поэтому сбой
    посреди экспорта оставляет набор без манифеста — состояние, которое
    никто не считает валидным и которое чинится повторным вызовом, — но
    никогда манифест с checksum ненаписанного файла и никогда прежний
    манифест рядом с уже переписанным содержимым ([REQ-010]: commit marker
    описывает то, что лежит рядом, иначе он не marker).

    Предусловие: `bootstrap_session(artifact_root)` уже вызван — `result/`
    создаёт только он ([REQ-002]), иначе вызов упадёт `FileNotFoundError`.
    Корень — журнал сессии (SPEC-002 §4.1): экспорт принадлежит сессии, и у
    двух сессий над одним репозиторием он свой.

    `ValueError`, если имя файла — не простое имя внутри `result/` или занято
    манифестом; `TypeError`, если `manifest` не сериализуем в JSON. И имена, и
    сам манифест проверяются до первой записи, поэтому неудачный вызов не
    оставляет ни частичного экспорта, ни прежнего манифеста рядом с уже
    перезаписанными файлами.

    `sha256` считается по тем же UTF-8 байтам, что уходят в файл, — контент
    сериализуется ровно один раз ([REQ-012]).
    """
    payloads = {name: content.encode("utf-8") for name, content in files.items()}
    for name in payloads:
        _check_file_name(name)

    checksums = {
        name: {"sha256": hashlib.sha256(data).hexdigest()}
        for name, data in payloads.items()
    }
    document = {**manifest, "files": checksums}
    serialized = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    )

    directory = result_dir(artifact_root)
    (directory / MANIFEST_NAME).unlink(missing_ok=True)
    for name, data in payloads.items():
        atomic_write(directory / name, data)
    atomic_write(directory / MANIFEST_NAME, serialized)


def _check_file_name(name: str) -> None:
    """Пропускает только простое имя файла внутри `result/`.

    Разделитель пути (в том числе ведущий — `Path / "/abs"` даёт абсолютный
    путь) увёл бы запись мимо `result/`. Имя манифеста зарезервировано: файл
    вызывающей стороны был бы затёрт манифестом, который при этом обещает его
    checksum.

    Сравнение с именем манифеста — регистронезависимое: на case-insensitive
    ФС (APFS, NTFS) `Manifest.JSON` — тот же файл, и точное сравнение пропустило
    бы ровно тот случай, ради которого имя зарезервировано. Правило одинаково на
    всех платформах: экспорт должен переноситься между ФС.
    """
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"имя файла результата должно быть простым именем: {name!r}")
    if name.casefold() == MANIFEST_NAME.casefold():
        raise ValueError(f"имя {name!r} зарезервировано за манифестом экспорта")
