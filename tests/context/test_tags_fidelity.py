"""Review-fix TASK-001: дословность содержимого под обезвреживанием.

`test_tags.py` байт-заморожен red-чекпоинтом, поэтому дыры, найденные на
review-проходе, закрываются отдельным модулем рядом (лок держит только
файл из `claim.selector`).

Что здесь пинится сверх `test_tags.py`:

* обезвреживание — вставка, а не вырезание: убрав из результата все ZWSP,
  получаем исходный `content` дословно. Мутант, который просто удаляет
  или экранирует вхождение метки, проходит адверсариальный тест
  («метка встречается ровно один раз»), но искажает цитату автора;
* пробелы по краям `content` не съедаются — `strip()` внутри обёртки
  переживал бы все тесты red-чекпоинта;
* метки не являются подстроками друг друга — иначе последовательные
  замены в `_neutralize` интерферировали бы.
"""

from disputatio.context.tags import _CLOSE_TAG, _OPEN_TAG, wrap_artifact_data

ZERO_WIDTH_SPACE = "\u200b"


def _strip_zwsp(text: str) -> str:
    """Убирает все ZWSP — обратная операция к обезвреживанию."""
    return text.replace(ZERO_WIDTH_SPACE, "")


def test_tags_are_not_substrings_of_each_other() -> None:
    """Предпосылка `_neutralize`: замены по меткам независимы."""
    assert _OPEN_TAG not in _CLOSE_TAG
    assert _CLOSE_TAG not in _OPEN_TAG


def test_neutralisation_inserts_and_never_deletes() -> None:
    """Снятие ZWSP возвращает содержимое дословно — ни один символ не потерян."""
    content = f"цитата с {_CLOSE_TAG} и {_OPEN_TAG} внутри"

    wrapped = wrap_artifact_data(content)

    assert ZERO_WIDTH_SPACE in wrapped
    body = _strip_zwsp(wrapped)[len(_OPEN_TAG) : -len(_CLOSE_TAG)]
    assert body == f"\n{content}\n"


def test_surrounding_whitespace_of_content_is_preserved() -> None:
    """`strip()` внутри обёртки запрещён: отступы автора — часть цитаты."""
    content = "   отступ слева и справа   \n\n"

    wrapped = wrap_artifact_data(content)

    assert wrapped == f"{_OPEN_TAG}\n{content}\n{_CLOSE_TAG}"


def test_empty_content_still_produces_a_well_formed_block() -> None:
    """Пустое содержимое — не спецслучай: блок остаётся закрытым."""
    wrapped = wrap_artifact_data("")

    assert wrapped.startswith(_OPEN_TAG)
    assert wrapped.splitlines()[-1] == _CLOSE_TAG
    assert wrapped.count(_CLOSE_TAG) == 1
