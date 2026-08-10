"""Разбор собранного промпта на artifact-блоки (TASK-007, [DESIGN-002]).

Инструмент проверок `test_hygiene.py`, а не сама проверка: префикса `test_`
нет, поэтому pytest этот модуль не собирает. Логика вынесена сюда потому,
что нужна обоим промптам сразу, и потому что «сколько в промпте валидных
границ и где они проходят» — вопрос со своим ответом, который лучше
сформулировать один раз и в одном месте.

Метки берутся из `disputatio.context.tags`, а не копируются: [ADR-001]
фиксирует, что они литералы, но их текст остаётся деталью реализации, и
переформулировка метки не должна ронять тесты гигиены.

Разбор синтаксический и ничего не исполняет: на вход строка, на выход
позиции. Модуль живёт под `tests/`, но I/O не делает и здесь — читать ему
нечего.
"""

from dataclasses import dataclass
from typing import Final

from disputatio.context.tags import _CLOSE_TAG, _OPEN_TAG

OPEN_TAG: Final = _OPEN_TAG
CLOSE_TAG: Final = _CLOSE_TAG

OPEN: Final = "open"
CLOSE: Final = "close"


@dataclass(frozen=True)
class Mark:
    """Вхождение метки: позиция в промпте и её вид (`OPEN`/`CLOSE`)."""

    index: int
    kind: str


@dataclass(frozen=True)
class Boundary:
    """Метки по порядку, замкнутые блоки и диагноз нарушений.

    `diagnosis` пуст ровно тогда, когда метки строго чередуются
    открытие→закрытие и последний блок закрыт. Непустая строка называет
    нарушения и их позиции — тест падает с указанием места, а не безымянным
    `False`.

    Блок — полудиапазон `[start, end)` от первого символа открывающей метки
    до последнего символа закрывающей включительно: сами метки принадлежат
    блоку, иначе «внутри блока» пришлось бы определять дважды.
    """

    marks: tuple[Mark, ...]
    blocks: tuple[tuple[int, int], ...]
    diagnosis: str

    def contains(self, index: int) -> bool:
        """Позиция `index` лежит внутри какого-нибудь artifact-блока."""
        return any(start <= index < end for start, end in self.blocks)

    def overlaps(self, start: int, end: int) -> bool:
        """Диапазон `[start, end)` пересекается хотя бы с одним блоком.

        Для статики оркестратора важно именно пересечение, а не попадание
        левой границы: заголовок, начавшийся до открывающей метки и заехавший
        за неё, по `contains` внутрь блока «не попал», но границу уже нарушил.
        """
        return any(
            start < block_end and block_start < end
            for block_start, block_end in self.blocks
        )


def scan(prompt: str) -> Boundary:
    """Разбирает `prompt` на artifact-блоки, проверяя чередование меток.

    Вложенное открытие, закрытие без открытия и незакрытый блок — три
    способа сломать границу; каждый попадает в `diagnosis` со своей
    позицией. Замкнутые блоки возвращаются и в присутствии нарушений:
    тесту статики нужны найденные границы даже там, где разбор уже
    признан битым.
    """
    marks = _marks(prompt)
    blocks: list[tuple[int, int]] = []
    problems: list[str] = []
    opened: int | None = None
    for mark in marks:
        if mark.kind == OPEN:
            if opened is not None:
                problems.append(f"открытие на {mark.index} внутри блока с {opened}")
                continue
            opened = mark.index
        elif opened is None:
            problems.append(f"закрытие на {mark.index} без открытия")
        else:
            blocks.append((opened, mark.index + len(CLOSE_TAG)))
            opened = None
    if opened is not None:
        problems.append(f"блок с {opened} не закрыт")
    return Boundary(tuple(marks), tuple(blocks), "; ".join(problems))


def find_all(text: str, needle: str) -> list[int]:
    """Позиции всех вхождений `needle` по возрастанию, включая наложенные.

    Шаг поиска — один символ, а не длина `needle`: перекрывающиеся
    вхождения тоже нарушения, и терять их нельзя.
    """
    positions: list[int] = []
    start = text.find(needle)
    while start >= 0:
        positions.append(start)
        start = text.find(needle, start + 1)
    return positions


def _marks(prompt: str) -> list[Mark]:
    """Метки обоих видов по возрастанию позиции.

    Сортировки по позиции достаточно, дополнительных оговорок не нужно: ни
    одна метка не является подстрокой другой, поэтому их вхождения не
    накладываются и на одной позиции двух меток быть не может.
    """
    marks = [
        Mark(index, kind)
        for tag, kind in ((OPEN_TAG, OPEN), (CLOSE_TAG, CLOSE))
        for index in find_all(prompt, tag)
    ]
    return sorted(marks, key=lambda mark: mark.index)
