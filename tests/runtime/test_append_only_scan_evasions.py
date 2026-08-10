"""Обходы скана [DESIGN-016]: локальные имена I3 и `os.open` ([REQ-016]).

Гарантия «runtime не глушит `RoundImmutableError`» держится на скане текста, а
`except` называет не класс, а локальную привязку к нему. Сверка написания вместо
смысла обходится строкой импорта: `from disputatio.events import
RoundImmutableError as _RIE` — и обработчик исчезает из отчёта, продолжая
глушить I3. То же с кортежем исключений, собранным в константу.

Здесь пинится именно замыкание имён — и обе его границы. Расширить его до «всякая
константа, у которой в аннотации есть `Exception`» значило бы объявить
обработчиком I3 schema-retry ([DESIGN-006]), который ловит совсем другое; такой
скан был бы отвергнут первым же прогоном по фактическому runtime.

`os.open` разбирается отдельно: по форме это метод, но второй его аргумент —
флаги, а первый — путь. Приняв путь за режим, скан читает `os.open("log.jsonl",
O_APPEND)` как чтение — то есть пропускает писателя.
"""

from pathlib import Path

import pytest

from disputatio.runtime.append_only import (
    scan_file_writes,
    scan_immutability_handlers,
    swallowed_immutability,
)

_ALIASED_IMPORT = (
    "from disputatio.events import RoundImmutableError as _RIE\n"
    "from disputatio.events import write_round_artifact\n"
    "\n"
    "\n"
    "def save(root, round_no, text):\n"
    "    try:\n"
    '        write_round_artifact(root, round_no, "review.json", text)\n'
    "    except _RIE:\n"
    "        return None\n"
)

_TUPLE_CONSTANT = (
    "from disputatio.events import RoundImmutableError, write_round_artifact\n"
    "\n"
    "IGNORED = (RoundImmutableError,)\n"
    "\n"
    "\n"
    "def save(root, round_no, text):\n"
    "    try:\n"
    '        write_round_artifact(root, round_no, "review.json", text)\n'
    "    except IGNORED:\n"
    "        return None\n"
)

_RENAMED_TWICE = (
    "from disputatio.events import RoundImmutableError as _RIE\n"
    "from disputatio.events import write_round_artifact\n"
    "\n"
    "_SILENCED = _RIE\n"
    "_ALSO = (ValueError, _SILENCED)\n"
    "\n"
    "\n"
    "def save(root, round_no, text):\n"
    "    try:\n"
    '        write_round_artifact(root, round_no, "review.json", text)\n'
    "    except _ALSO:\n"
    "        return None\n"
)

_DOTTED_ALIAS = (
    "import disputatio.events.rounds as _rounds\n"
    "from disputatio.events import write_round_artifact\n"
    "\n"
    "\n"
    "def save(root, round_no, text):\n"
    "    try:\n"
    '        write_round_artifact(root, round_no, "review.json", text)\n'
    "    except _rounds.RoundImmutableError:\n"
    "        return None\n"
)

# Форма `retry.SCHEMA_INVALID_ERRORS`: `Exception` стоит в АННОТАЦИИ, а ловится
# кортеж совсем других ошибок. Обработчиком I3 такой `except` не является.
_ANNOTATED_UNRELATED_ERRORS = (
    "from disputatio.contracts import ProposalParseError\n"
    "\n"
    "SCHEMA_INVALID_ERRORS: tuple[type[Exception], ...] = (ProposalParseError,)\n"
    "\n"
    "\n"
    "def parse(text):\n"
    "    try:\n"
    "        return load(text)\n"
    "    except SCHEMA_INVALID_ERRORS:\n"
    "        return None\n"
)

_OS_OPEN_APPEND = (
    "import os\n"
    "\n"
    "\n"
    "def dump(line: str) -> None:\n"
    '    fd = os.open("log.jsonl", os.O_WRONLY | os.O_APPEND)\n'
    "    os.close(fd)\n"
)


def _rogue_package(tmp_path: Path, name: str, source: str) -> Path:
    """Дерево из одного модуля `name` с исходником `source` — площадка скана."""
    package = tmp_path / "evasion_package"
    package.mkdir(exist_ok=True)
    (package / name).write_text(source, encoding="utf-8")
    return package


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("aliased.py", _ALIASED_IMPORT),
        ("tuple_constant.py", _TUPLE_CONSTANT),
        ("renamed_twice.py", _RENAMED_TWICE),
        ("dotted.py", _DOTTED_ALIAS),
    ],
    ids=["import-as", "tuple-constant", "renamed-twice", "dotted-alias"],
)
def test_i3_swallowed_under_a_local_name_is_still_reported(
    tmp_path: Path, name: str, source: str
) -> None:
    """`except` по локальной привязке I3 — то же поглощение, тот же отчёт.

    Все четыре модуля глушат `RoundImmutableError` без единого `raise`, и ни
    один не называет его по имени в `except`. Скан, сверяющий написание,
    вернул бы по ним пустой список — то есть объявил бы runtime чистым ровно
    в том случае, ради которого проверка и заведена.
    """
    package = _rogue_package(tmp_path, name, source)

    assert [handler.function for handler in swallowed_immutability(package)] == ["save"]


def test_a_constant_annotated_with_exception_is_not_an_i3_handler(
    tmp_path: Path,
) -> None:
    """Граница замыкания: `Exception` в аннотации ничего не ловит.

    Обратная сторона предыдущего теста. Замыкание, собранное по всему
    присваиванию, а не по его значению, приняло бы `SCHEMA_INVALID_ERRORS` за
    имя I3 — и schema-retry ([DESIGN-006]) оказался бы «вторым мнением о том,
    когда закрытый раунд можно переписать», которым он не является.
    """
    package = _rogue_package(tmp_path, "retryish.py", _ANNOTATED_UNRELATED_ERRORS)

    assert scan_immutability_handlers(package) == []


def test_os_open_in_append_mode_is_reported_as_a_write(tmp_path: Path) -> None:
    """`os.open(path, flags)` — запись; путь режимом не считается.

    У `os.open` второй аргумент — флаги, а не режим. Прочитанный как режим
    путь `"log.jsonl"` не содержит ни `w`, ни `a`, ни `x`, ни `+`, поэтому
    открытие журнала на дозапись прошло бы скан как чтение.
    """
    package = _rogue_package(tmp_path, "os_writer.py", _OS_OPEN_APPEND)

    writes = scan_file_writes(package)

    assert [write.target for write in writes] == ["os.open"]
    assert writes[0].mode == "os.O_WRONLY | os.O_APPEND"
