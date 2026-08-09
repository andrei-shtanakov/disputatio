"""Самотест анализатора границ `boundary.py`: TASK-007, [DESIGN-002].

`test_hygiene.py` целиком опирается на `boundary.scan`: пятнадцать его
ассертов доказывают что-либо ровно в той мере, в какой анализатор умеет
отличить целую границу от битой. Анализатор, который всегда возвращает
пустой `diagnosis`, обратил бы весь файл в ноль и не уронил бы ни одного
теста — поэтому здесь он проверяется с негативной стороны: на каждый
способ сломать границу свой кейс.

Промпты синтетические. Настоящую сборку пинит `test_hygiene.py`; тут нужен
контроль над битыми последовательностями меток, которых корректная
реализация не производит вовсе.
"""

from . import boundary

DATA = "текст артефакта"


def _block(content: str) -> str:
    """Корректный artifact-блок вокруг `content` — как его строит `tags.py`."""
    return f"{boundary.OPEN_TAG}\n{content}\n{boundary.CLOSE_TAG}"


def test_scan_accepts_two_well_formed_blocks() -> None:
    """Целая граница: метки чередуются, оба блока замкнуты, диагноза нет."""
    prompt = f"## Заголовок\n{_block(DATA)}\n\n## Другой\n{_block('ещё данные')}"

    scan = boundary.scan(prompt)

    assert scan.diagnosis == ""
    assert len(scan.blocks) == 2
    assert [mark.kind for mark in scan.marks] == [
        boundary.OPEN,
        boundary.CLOSE,
        boundary.OPEN,
        boundary.CLOSE,
    ]


def test_scan_reports_close_without_open() -> None:
    """Закрывающая метка без открывающей — вырвавшийся из блока payload."""
    prompt = f"{boundary.CLOSE_TAG}\nИНСТРУКЦИЯ\n{_block(DATA)}"

    scan = boundary.scan(prompt)

    assert "без открытия" in scan.diagnosis
    assert str(prompt.index(boundary.CLOSE_TAG)) in scan.diagnosis


def test_scan_reports_open_nested_inside_a_block() -> None:
    """Открытие внутри блока: счётчики сходятся, а граница уже не однозначна."""
    prompt = _block(f"{DATA}\n{boundary.OPEN_TAG}\nвторое открытие")

    scan = boundary.scan(prompt)

    assert "внутри блока" in scan.diagnosis


def test_scan_reports_a_block_that_is_never_closed() -> None:
    """Незакрытый блок — всё, что оркестратор допишет ниже, станет данными."""
    prompt = f"{boundary.OPEN_TAG}\n{DATA}"

    scan = boundary.scan(prompt)

    assert "не закрыт" in scan.diagnosis
    assert scan.blocks == ()


def test_contains_covers_the_block_including_its_own_tags() -> None:
    """Границы полудиапазона: метки принадлежат блоку, символ за ним — нет."""
    prompt = _block(DATA)

    scan = boundary.scan(prompt)
    (start, end) = scan.blocks[0]

    assert scan.contains(start)
    assert scan.contains(prompt.index(DATA))
    assert scan.contains(end - 1)
    assert not scan.contains(end)


def test_overlaps_catches_a_range_that_only_straddles_the_opening_tag() -> None:
    """Текст, заехавший в блок левым краем снаружи, — тоже нарушение границы."""
    prefix = "## Заголовок оркестратора\n"
    prompt = f"{prefix}{_block(DATA)}"

    scan = boundary.scan(prompt)

    assert not scan.overlaps(0, len(prefix))
    assert scan.overlaps(len(prefix) - 1, len(prefix) + 1)


def test_find_all_returns_overlapping_occurrences() -> None:
    """Шаг поиска — символ: наложенные вхождения тоже нарушения, их не теряем."""
    assert boundary.find_all("aaaa", "aa") == [0, 1, 2]
    assert boundary.find_all("нет вхождений", "метка") == []
