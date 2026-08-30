r"""Парсер ссылок в документах и нормализация якорей (SPEC-002 §6).

`parse_doc_refs` — чистая функция без файлового I/O: разбирает текст
документа на `DocRef` по замкнутому набору однозначно распознаваемых форм.
Существование целей и containment — забота `doc_gates` (это парсер, а не
гейт).

Распознаваемые формы (полный список, эвристики запрещены — см. §6):

- Markdown inline-ссылка ``[text](target)``, опционально с заголовком
  (``(target "title")``) и фрагментом (``target#anchor``).
- Markdown reference-ссылка полной/collapsed формы — ``[text][ref]`` или
  ``[text][]`` — с определением ``[ref]: target`` где-то в документе.
  Bare shortcut-ссылка ``[ref]`` без второй пары скобок НЕ распознаётся: в
  текстах этого проекта ``[DESIGN-002]``, ``[REQ-004]`` и т. п. — метки
  трассируемости, а не ссылки, и отличить их от настоящего shortcut
  reference эвристически нельзя. Ссылка без найденного определения тоже не
  порождает `DocRef` — цель не выводима, а не наоборот, — но и молча не
  исчезает: она попадает в `ParsedDocument.unresolved` (см. ниже).
- Автоссылка ``<target>`` — только когда `target` не начинается с
  URI-схемы (``scheme:`` — внешняя ссылка вне области действия гейтов) и
  содержит `/` или `.` (иначе неотличимо от HTML-тега вроде ``<br>``).
- Путь и ``file.py:42`` в inline-code (одинарные обратные кавычки):
  признак пути — не «похоже на путь», а наличие `/` и расширения.
  ``file.py:42`` может нести якорный текст строки — ровно в форме
  ``` `file.py:42` («текст строки») ``` (кавычки-«ёлочки», сразу после
  code-спана, без иного текста между ними): `expected_text` DocRef'а
  заполняется этим текстом, проверка дрейфа — забота `doc_gates`
  (`doc-line-refs`, задача 8). Форма без хвостовых кавычек оставляет
  `expected_text` пустым — гейт тогда проверяет только существование
  строки.
- Декларация файла задачи — bullet, начинающийся с ``Modify:``/``Test:``/
  ``Create:`` (может продолжаться на следующих строках — этот же файл
  оформлен так же). Пути внутри — inline-code. ``Modify``/``Test`` →
  `declared_existing` (утверждение о существующем), ``Create`` →
  `declared_planned` (объявление намерения, см. `doc_gates.gate_doc_paths`).

Всё прочее не порождает `DocRef` вовсе.

**Экранирование снимает форму целиком, и правило одно на все формы.**
Открывающий символ, перед которым стоит НЕЧЁТНОЕ число обратных слешей
(``\[plan][missing]``, ``\[текст](t.md)``, ``\<t.md>``, ``\`t.md` ``,
и он же в путях деклараций), — обычный текст: документ показывает, КАК
форма пишется, а не пользуется ею. Считается чётность, потому что ``\\``
экранирует сам слеш, и ссылка после него остаётся ссылкой. Разбирать
экранирование в одной форме и не разбирать в остальных нельзя: цена
одинаковых промахов разная лишь по громкости — у reference-формы это
ложный `unresolved_ref` (`warning` в отчёте doc-ревьюеру), у пути в
inline-code — уже `fail` гейта `doc-paths`. Дальше этой точки парсер за
markdown не гонится: пересчёта пар бэктиков за экранированным спаном он не
делает — он распознаватель замкнутого набора форм, а не renderer.

**Fenced code block снимает ВСЕ формы разом, и это не эвристика, а
рендеринг.** Строки внутри ограды (``` ``` ``` ```/``` ~~~ ```, CommonMark)
документ показывает, а не использует: в отрендеренном виде это буквальный
текст. Правило одно на весь модуль — на inline-формы, на определения
reference-ссылок, на bullet'ы деклараций и на заголовки (`iter_headings`), —
потому что цена промаха разная лишь по направлению. У ссылок это ложный
`fail` обязательного `doc-paths` на вымышленной цели примера, то есть
корректный документ не сходится никогда; у заголовков — наоборот, ложный
`pass` `doc-anchors` на якорь, ведущий в секцию, которой в документе нет.
Пайплайн полирует спеки и планы, а те состоят из примеров в оградах: §3.2
самой SPEC-002 — TOML-блок, каждая строка комментария в котором совпадает
с шаблоном ATX-заголовка. Ограда, не закрытая до конца документа, держит
остаток под собой — так его рендерит CommonMark, и «дочитать до конца как
прозу» означало бы проверять не тот документ, который увидит человек.
Дальше этого парсер за блочной структурой markdown по-прежнему не гонится:
indented code block (четыре пробела) он не распознаёт — отличить его от
продолжения элемента списка можно только полным блочным разбором, а
ошибка в эту сторону вернула бы ложные `fail` на обычных вложенных
списках.

**`DocRef` — не единственный результат разбора.** `parse_document` возвращает
пару: распознанные ссылки и `unresolved` — reference-ссылки правильной формы
``[text][ref]``, для которых определения в документе нет. Ссылка без цели —
не `DocRef` (резолвить нечего), но и не ничто: `doc-links` — baseline-гейт
детерминированного критерия сходимости, и `pass` по документу, где такая
форма прошла молча, означал бы «проверено» про непроверенное. Bare-метка
``[REQ-004]`` сюда не попадает: она не ссылка ни в каком виде, и warning на
каждой метке трассируемости был бы шумом, а не находкой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

DocRefKind = Literal[
    "md_link",
    "autolink",
    "code_path",
    "code_line_ref",
    "declared_existing",
    "declared_planned",
]


@dataclass(frozen=True, slots=True)
class DocRef:
    """Одна распознанная ссылка/декларация пути в документе.

    `line` — строка документа, где встретилась форма (1-based). Для
    `code_line_ref` целевая строка (``:42``) остаётся частью `target`
    целиком — её разбор относится к `doc-line-refs`, вне задачи 7.
    """

    kind: DocRefKind
    target: str
    line: int
    anchor: str | None = None
    expected_text: str | None = None


@dataclass(frozen=True, slots=True)
class UnresolvedRef:
    """Reference-ссылка правильной формы, определения для которой нет.

    `label` — метка как она написана (регистр сохранён: сообщение гейта
    должно совпасть с текстом документа, а не с ключом поиска). `line` —
    строка использования, 1-based, как у `DocRef`.
    """

    label: str
    line: int


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Полный результат разбора: распознанное и увиденное, но не выводимое."""

    refs: list[DocRef]
    unresolved: list[UnresolvedRef]


_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_CODE_LINE_REF_RE = re.compile(r"^[\w./-]*/[\w./-]+\.\w+:\d+$")
_CODE_PATH_RE = re.compile(r"^[\w./-]*/[\w./-]+\.\w+$")
_MD_INLINE_LINK_RE = re.compile(
    r'\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+"[^"]*")?\)'
)
_MD_REF_LINK_RE = re.compile(r"\[(?P<text>[^\]]+)\]\[(?P<ref>[^\]]*)\]")
_MD_REF_DEF_RE = re.compile(r"^\s{0,3}\[(?P<label>[^\]]+)\]:\s*(?P<target>\S+)")
_AUTOLINK_RE = re.compile(r"<([^<>\s]+)>")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_BULLET_RE = re.compile(r"^\s*[-*]\s*(Modify|Test|Create):\s*(.*)$")
_BULLET_START_RE = re.compile(r"^\s*[-*]\s")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_TRAILING_QUOTE_RE = re.compile(r"^\s*\(«([^»]*)»\)")

_PUNCTUATION_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def _is_escaped(line: str, start: int) -> bool:
    r"""Экранирован ли символ в позиции `start` обратными слешами перед ним.

    Считается ЧЁТНОСТЬ, а не наличие: `\[` — экранированная скобка (форма
    остаётся текстом), `\\[` — экранированный слеш плюс обычная скобка (форма
    остаётся ссылкой). Проверка «есть ли слеш перед» скрыла бы от гейтов
    настоящую ссылку, стоящую после литерального `\`.
    """
    backslashes = 0
    index = start - 1
    while index >= 0 and line[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _fenced_flags(lines: list[str]) -> list[bool]:
    """Для каждой строки — лежит ли она в fenced code block'е (CommonMark).

    Ограждающие строки помечены наравне с содержимым: инфо-строка открытия
    (``` ```markdown ```) — тоже не проза. Ограда закрывается оградой ТОГО
    ЖЕ символа, ДЛИНОЙ НЕ МЕНЬШЕ открывающей и без инфо-строки: документация
    про markdown показывает блок внутри блока ровно этим приёмом, и
    закрытие по факту трёх символов разорвало бы внешний пример пополам.
    Инфо-строка backtick-ограды не может содержать бэктик (иначе абзац с
    двумя спанами открывал бы блок и глушил остаток документа); у
    tilde-ограды такого ограничения нет.

    Незакрытая ограда держит остаток документа — так его рендерит
    CommonMark. Это единственное место, где парсер знает про блочную
    структуру markdown, и знает он ровно столько, сколько нужно, чтобы не
    считать показ формы её использованием.
    """
    flags = [False] * len(lines)
    opening: str | None = None
    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if opening is None:
            if match is None:
                continue
            marker = match.group("fence")
            if marker[0] == "`" and "`" in match.group("info"):
                continue
            opening = marker
            flags[index] = True
            continue
        flags[index] = True
        if match is None:
            continue
        closing = match.group("fence")
        if (
            closing[0] == opening[0]
            and len(closing) >= len(opening)
            and not match.group("info").strip()
        ):
            opening = None
    return flags


def parse_doc_refs(text: str) -> list[DocRef]:
    """Разбирает `text` на `DocRef` по замкнутому набору форм (см. модуль)."""
    return parse_document(text).refs


def parse_document(text: str) -> ParsedDocument:
    """Полный разбор: `DocRef`-ы и неразрешённые reference-ссылки.

    Один проход на оба результата намеренно: маскирование inline-code,
    список определений, состояние ограды и строки, съеденные декларациями
    `Modify:`/`Create:`, обязаны быть одними и теми же для обеих половин.
    Второй проход по своей копии этих правил разошёлся бы с первым — и
    разошёлся бы именно там, где расхождение выглядит как отсутствие находки.
    """
    lines = text.splitlines()
    fenced = _fenced_flags(lines)
    definitions = _collect_link_definitions(lines, fenced)
    declared_refs, consumed_lines = _parse_declared_paths(lines, fenced)
    refs: list[DocRef] = list(declared_refs)
    unresolved: list[UnresolvedRef] = []
    for lineno, raw_line in enumerate(lines, start=1):
        if fenced[lineno - 1]:
            # Содержимое ограды — показ формы, а не форма (см. модуль).
            continue
        if lineno in consumed_lines:
            # Строка уже разобрана как Modify:/Test:/Create: — те же
            # backtick-пути не считаются повторно как обычный code_path.
            continue
        masked, code_spans = _mask_inline_code(raw_line)
        for content, expected_text in code_spans:
            ref = _match_code_span(content, lineno, expected_text)
            if ref is not None:
                refs.append(ref)
        line_refs, line_unresolved = _match_md_links(masked, lineno, definitions)
        refs.extend(line_refs)
        unresolved.extend(line_unresolved)
        refs.extend(_match_autolinks(masked, lineno))
    return ParsedDocument(refs=refs, unresolved=unresolved)


def github_slug(heading: str, seen: dict[str, int]) -> str:
    """Нормализует заголовок в GitHub-подобный якорь.

    Правила (зафиксированы тестами, не renderer'ом): percent-decoding
    входа, casefold, снятие пунктуации (``\\w``/пробел/дефис — Unicode-буквы
    и цифры сохраняются как есть), пробелы → дефис, суффиксы ``-1``/``-2``
    для повторов через общий `seen` (первое вхождение — без суффикса).
    """
    decoded = unquote(heading)
    folded = decoded.casefold()
    stripped = _PUNCTUATION_RE.sub("", folded)
    slug = _WHITESPACE_RE.sub("-", stripped.strip())
    count = seen.get(slug, 0)
    seen[slug] = count + 1
    if count == 0:
        return slug
    return f"{slug}-{count}"


def iter_headings(text: str) -> list[tuple[int, str]]:
    """Возвращает `(line, heading_text)` для ATX-заголовков документа.

    Строки внутри fenced code block'а заголовков не дают: `# comment` в
    TOML-примере совпадает с шаблоном ATX, но секции в отрендеренном
    документе не создаёт. Разница здесь направлена в fail-open: лишний
    slug — это `pass` `doc-anchors` на якорь, ведущий в никуда.
    """
    lines = text.splitlines()
    fenced = _fenced_flags(lines)
    headings: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=1):
        if fenced[lineno - 1]:
            continue
        match = _ATX_HEADING_RE.match(line)
        if match:
            headings.append((lineno, match.group(2)))
    return headings


def _mask_inline_code(line: str) -> tuple[str, list[tuple[str, str | None]]]:
    """Заменяет inline-code спаны пробелами; возвращает (строку, спаны).

    Каждый спан — пара `(content, expected_text)`: `expected_text` — текст
    из кавычек-«ёлочек» сразу после спана (форма ``` `f.py:42` («текст») ```,
    см. модуль), иначе `None`. `match.end()` берётся из исходной `line` —
    позиции спанов в ней не сдвигаются заменой на пробелы той же длины.
    """
    spans: list[tuple[str, str | None]] = []

    def _replace(match: re.Match[str]) -> str:
        if _is_escaped(line, match.start()):
            # Экранированный бэктик спана не открывает: текст остаётся
            # текстом — и в маске тоже, иначе формы под ним пропали бы.
            return match.group(0)
        tail = line[match.end() :]
        quote = _TRAILING_QUOTE_RE.match(tail)
        spans.append((match.group(1), quote.group(1) if quote else None))
        return " " * len(match.group(0))

    return _INLINE_CODE_RE.sub(_replace, line), spans


def _match_code_span(
    content: str, lineno: int, expected_text: str | None
) -> DocRef | None:
    if _CODE_LINE_REF_RE.match(content):
        return DocRef(
            kind="code_line_ref",
            target=content,
            line=lineno,
            expected_text=expected_text,
        )
    if _CODE_PATH_RE.match(content):
        return DocRef(kind="code_path", target=content, line=lineno)
    return None


def _split_target_anchor(raw: str) -> tuple[str, str | None]:
    path, sep, anchor = raw.partition("#")
    return path, (anchor if sep else None)


def _collect_link_definitions(lines: list[str], fenced: list[bool]) -> dict[str, str]:
    """Определения reference-ссылок; строки внутри ограды не определяют ничего.

    Определение в примере — не определение: собери его, и ссылка ВНЕ
    примера получила бы цель, которой документ не называл.
    """
    definitions: dict[str, str] = {}
    for index, line in enumerate(lines):
        if fenced[index]:
            continue
        match = _MD_REF_DEF_RE.match(line)
        if match:
            definitions[match.group("label").casefold()] = match.group("target")
    return definitions


def _match_md_links(
    line: str, lineno: int, definitions: dict[str, str]
) -> tuple[list[DocRef], list[UnresolvedRef]]:
    refs: list[DocRef] = []
    unresolved: list[UnresolvedRef] = []
    for match in _MD_INLINE_LINK_RE.finditer(line):
        if _is_escaped(line, match.start()):
            continue
        path, anchor = _split_target_anchor(match.group("target"))
        refs.append(DocRef(kind="md_link", target=path, line=lineno, anchor=anchor))
    for match in _MD_REF_LINK_RE.finditer(line):
        if _is_escaped(line, match.start()):
            continue
        written = match.group("ref") or match.group("text")
        raw_target = definitions.get(written.casefold())
        if raw_target is None:
            # Определение не найдено — цель не выводима, `DocRef` не
            # порождается; но форма увидена, и гейт обязан о ней сказать.
            unresolved.append(UnresolvedRef(label=written, line=lineno))
            continue
        path, anchor = _split_target_anchor(raw_target)
        refs.append(DocRef(kind="md_link", target=path, line=lineno, anchor=anchor))
    return refs, unresolved


def _match_autolinks(line: str, lineno: int) -> list[DocRef]:
    refs: list[DocRef] = []
    for match in _AUTOLINK_RE.finditer(line):
        if _is_escaped(line, match.start()):
            continue
        content = match.group(1)
        if _URI_SCHEME_RE.match(content):
            continue  # URI-схема — внешняя ссылка, вне области гейтов
        if "/" not in content and "." not in content:
            continue  # неотличимо от HTML-тега вроде <br>/<Foo>
        path, anchor = _split_target_anchor(content)
        refs.append(DocRef(kind="autolink", target=path, line=lineno, anchor=anchor))
    return refs


def _parse_declared_paths(
    lines: list[str], fenced: list[bool]
) -> tuple[list[DocRef], set[int]]:
    """Декларации `Modify:`/`Test:`/`Create:`; ограда — не место для них.

    Ограда обрывает и продолжение bullet'а: пример, стоящий сразу за
    декларацией, — соседний блок, а не её хвост.
    """
    refs: list[DocRef] = []
    consumed: set[int] = set()
    index = 0
    total = len(lines)
    while index < total:
        if fenced[index]:
            index += 1
            continue
        match = _BULLET_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        kind: DocRefKind = (
            "declared_planned" if match.group(1) == "Create" else "declared_existing"
        )
        bullet_lines = [(index + 1, match.group(2))]
        cursor = index + 1
        while cursor < total:
            candidate = lines[cursor]
            if fenced[cursor] or not candidate.strip():
                break
            if _BULLET_RE.match(candidate) or _BULLET_START_RE.match(candidate):
                break
            bullet_lines.append((cursor + 1, candidate))
            cursor += 1
        for lineno, content in bullet_lines:
            consumed.add(lineno)
            for backtick_match in _BACKTICK_RE.finditer(content):
                if _is_escaped(content, backtick_match.start()):
                    continue
                refs.append(
                    DocRef(kind=kind, target=backtick_match.group(1), line=lineno)
                )
        index = cursor
    return refs, consumed
