"""Парсер ссылок в документах и нормализация якорей (SPEC-002 §6).

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
  порождает `DocRef` — цель не выводима, а не наоборот.
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
_TRAILING_QUOTE_RE = re.compile(r"^\s*\(«([^»]*)»\)")

_PUNCTUATION_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def parse_doc_refs(text: str) -> list[DocRef]:
    """Разбирает `text` на `DocRef` по замкнутому набору форм (см. модуль)."""
    lines = text.splitlines()
    definitions = _collect_link_definitions(lines)
    declared_refs, consumed_lines = _parse_declared_paths(lines)
    refs: list[DocRef] = list(declared_refs)
    for lineno, raw_line in enumerate(lines, start=1):
        if lineno in consumed_lines:
            # Строка уже разобрана как Modify:/Test:/Create: — те же
            # backtick-пути не считаются повторно как обычный code_path.
            continue
        masked, code_spans = _mask_inline_code(raw_line)
        for content, expected_text in code_spans:
            ref = _match_code_span(content, lineno, expected_text)
            if ref is not None:
                refs.append(ref)
        refs.extend(_match_md_links(masked, lineno, definitions))
        refs.extend(_match_autolinks(masked, lineno))
    return refs


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
    """Возвращает `(line, heading_text)` для ATX-заголовков документа."""
    headings: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
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


def _collect_link_definitions(lines: list[str]) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for line in lines:
        match = _MD_REF_DEF_RE.match(line)
        if match:
            definitions[match.group("label").casefold()] = match.group("target")
    return definitions


def _match_md_links(
    line: str, lineno: int, definitions: dict[str, str]
) -> list[DocRef]:
    refs: list[DocRef] = []
    for match in _MD_INLINE_LINK_RE.finditer(line):
        path, anchor = _split_target_anchor(match.group("target"))
        refs.append(DocRef(kind="md_link", target=path, line=lineno, anchor=anchor))
    for match in _MD_REF_LINK_RE.finditer(line):
        label = (match.group("ref") or match.group("text")).casefold()
        raw_target = definitions.get(label)
        if raw_target is None:
            continue  # определение не найдено — цель не выводима
        path, anchor = _split_target_anchor(raw_target)
        refs.append(DocRef(kind="md_link", target=path, line=lineno, anchor=anchor))
    return refs


def _match_autolinks(line: str, lineno: int) -> list[DocRef]:
    refs: list[DocRef] = []
    for match in _AUTOLINK_RE.finditer(line):
        content = match.group(1)
        if _URI_SCHEME_RE.match(content):
            continue  # URI-схема — внешняя ссылка, вне области гейтов
        if "/" not in content and "." not in content:
            continue  # неотличимо от HTML-тега вроде <br>/<Foo>
        path, anchor = _split_target_anchor(content)
        refs.append(DocRef(kind="autolink", target=path, line=lineno, anchor=anchor))
    return refs


def _parse_declared_paths(lines: list[str]) -> tuple[list[DocRef], set[int]]:
    refs: list[DocRef] = []
    consumed: set[int] = set()
    index = 0
    total = len(lines)
    while index < total:
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
            if not candidate.strip():
                break
            if _BULLET_RE.match(candidate) or _BULLET_START_RE.match(candidate):
                break
            bullet_lines.append((cursor + 1, candidate))
            cursor += 1
        for lineno, content in bullet_lines:
            consumed.add(lineno)
            for backtick_match in _BACKTICK_RE.finditer(content):
                refs.append(
                    DocRef(kind=kind, target=backtick_match.group(1), line=lineno)
                )
        index = cursor
    return refs, consumed
