"""Чтение Markdown плана и спеки для гейта wiring CommonMark-парсером (§5.3).

`MarkdownPlanReader` реализует протокол `disputatio.verifier.wiring.PlanReader`:
гейт `verifier` не импортирует сторонних пакетов (INV-10) и видит Markdown
только через этот протокол, а разбор делает `markdown-it-py` с пресетом
`commonmark`. Модуль живёт вне `verifier`, чтобы граница INV-10 оставалась
буквальной; `cli` создаёт читателя и передаёт его в `check_wiring`.

Гейт обязан видеть в плане ровно то, что видит читатель отрендеренного
документа: ручной разбор разметки раз за разом находил случаи, где гейт
«видел» текст, которого в рендере нет. Поэтому границы блоков, заголовков,
HTML и определений ссылок определяет парсер, а здесь — только выбор токенов.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence

from markdown_it import MarkdownIt
from markdown_it.token import Token

from disputatio.verifier import WiringInputError

# Заголовок задачи: видимый текст заголовка `h3` начинается с
# `Задача N:`/`Task N:`. Пробел между словами и вокруг номера — любой
# юникодный (str-паттерн `\s` берёт NBSP, em space, narrow NBSP и т. п., не
# только `[ \t]`, — §5.3). Номер — только ASCII-цифры: `\d` матчит и `١`,
# а `int("١") == 1`.
_TASK_TEXT_RE = re.compile(r"\s*(?:Задача|Task)\s+([0-9]+)\s*:")
_TASK_TAG = "h3"
_BOUNDARY_TAGS = frozenset({"h1", "h2", "h3"})
# Переводы строки внутри абзаца — один пробел: фрагменты по разные стороны
# переноса в одно «место» не склеиваются.
_BREAKS = frozenset({"softbreak", "hardbreak"})
_TEXT_TOKENS = frozenset({"text", "code_inline"})


class MarkdownPlanReader:
    """Читатель плана и спеки для гейта wiring на `markdown-it-py` (§5.3)."""

    def __init__(self) -> None:
        self._parser = MarkdownIt("commonmark")

    def fenced_blocks(self, text: str, info: str) -> list[str]:
        """Содержимое настоящих fenced-блоков с info-строкой ровно `info`.

        Фенс определяет парсер: строка фенса внутри другого фенса, HTML-блока
        или комментария — литерал, а не блок; блок в цитате или пункте списка
        — блок. Незакрытый фенс тянется до конца документа (CommonMark).
        """
        return [
            token.content
            for token in self._parser.parse(text)
            if token.type == "fence" and token.info == info
        ]

    def task_sections(self, text: str) -> Mapping[int, str]:
        """Номер задачи → видимый текст её раздела (§5.3).

        Заголовок задачи — `h3` верхнего уровня документа (не в цитате и не в
        пункте списка), чей видимый текст начинается с `Задача N:`/`Task N:`.
        Раздел тянется до следующего заголовка `h1`–`h3` верхнего уровня.
        Повтор номера — `WiringInputError` (код 2).
        """
        tokens = self._parser.parse(text)
        sections: dict[int, str] = {}
        for start, number in _task_headings(tokens):
            if number in sections:
                raise WiringInputError(
                    f"план: несколько заголовков задачи №{number} (§5.3)"
                )
            sections[number] = _visible_text(
                tokens[start : _section_end(tokens, start)]
            )
        return sections


def _task_headings(tokens: Sequence[Token]) -> Iterator[tuple[int, int]]:
    """Индексы `heading_open` заголовков задач и их номера, в порядке документа."""
    for index, token in enumerate(tokens):
        if not _is_top_heading(token, frozenset({_TASK_TAG})):
            continue
        match = _TASK_TEXT_RE.match(_inline_text(tokens[index + 1]))
        if match is not None:
            yield index, int(match.group(1))


def _section_end(tokens: Sequence[Token], start: int) -> int:
    """Индекс следующего `h1`–`h3` верхнего уровня после `start` или конец."""
    return next(
        (
            index
            for index in range(start + 1, len(tokens))
            if _is_top_heading(tokens[index], _BOUNDARY_TAGS)
        ),
        len(tokens),
    )


def _is_top_heading(token: Token, tags: frozenset[str]) -> bool:
    """`heading_open` с тегом из `tags` вне цитат и пунктов списка."""
    return token.type == "heading_open" and token.level == 0 and token.tag in tags


def _visible_text(tokens: Sequence[Token]) -> str:
    """Видимый текст абзацев и заголовков вне цитат; блоки — через `\\n`.

    Учитываются только `inline`-токены (содержимое абзацев и заголовков, в
    том числе в пунктах списка). Всё внутри цитаты не учитывается; `fence`,
    `code_block`, `html_block` inline-содержимого не несут, а определения
    ссылок парсер поглощает без токенов.
    """
    lines: list[str] = []
    quote_depth = 0
    for token in tokens:
        if token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        elif token.type == "inline" and quote_depth == 0:
            lines.append(_inline_text(token))
    return "\n".join(lines)


def _inline_text(token: Token) -> str:
    """Отрендеренный текст inline-токена: `text` и `code_inline`, переносы — пробел.

    Ссылка даёт только видимую метку (её дочерние `text`), но не адрес и не
    `title`; картинка, `html_inline` и маркеры выделения текста не дают.
    """
    parts: list[str] = []
    for child in token.children or []:
        if child.type in _TEXT_TOKENS:
            parts.append(child.content)
        elif child.type in _BREAKS:
            parts.append(" ")
    return "".join(parts)
