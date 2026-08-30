"""Doc-гейты baseline `document`-режима (SPEC-002 §6): `doc-paths`,
`doc-links`, `doc-anchors`, `doc-line-refs`, `doc-scope`.

Первые три и `doc-line-refs` — детерминированные гейты поверх
`parse_doc_refs` (`doc_refs.py`), без запуска внешних процессов: статус
вычисляется напрямую из файловой системы. `doc-scope` — единственный гейт
без документа на входе: он разбирает пути из текста патча раунда
(`changes.patch`) и роняет раунд на любом пути вне `allowed` — контурная
граница (spec-контур пускает только `spec_path`, pair-контур — только
`plan_path`), а не хук для расширения.

**Утверждение о существовании отличается от объявления намерения** (§6):
`gate_doc_paths` роняет раунд только на формах, которые утверждают
существование (`md_link`, `autolink`, `code_line_ref`, `declared_existing`).
`declared_planned` (путь после ``Create:``) — объявление намерения:
отсутствие — норма, существование — `warning` (задача объявляет создание
уже существующего). Прочий `code_path` при отсутствии — тоже `warning`, не
`fail`: спека, проектирующая ещё не написанный модуль, иначе не сошлась бы
никогда.

**Третий вид `warning` — форма, которую гейт проверить не может.**
Reference-ссылка ``[text][ref]`` без определения не даёт цели, то есть не
порождает `DocRef` вовсе; `doc-links` записывает её кодом `unresolved_ref`,
оставляя статус `pass`. Молчать нельзя: гейт входит в детерминированный
критерий сходимости, и `pass` без единого следа означал бы, что документ
проверен целиком, — а проверен он был не весь.

**База резолвинга различается по виду `DocRef` (фикс-раунд 1).**
`md_link`/`autolink` — синтаксис относительных Markdown-ссылок: по
CommonMark/GitHub они резолвятся относительно **каталога документа**, а
не корня репозитория (`_resolve_relative_to_doc`). `declared_existing`/
`declared_planned`/`code_path`/`code_line_ref` пишутся в спеках/планах как
пути от корня репозитория и резолвятся через `resolve_inside` — от него же.
В обоих случаях containment (`..`/symlink-выход) проверяется относительно
`repo_root` — база соединения и граница удержания разведены сознательно.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from disputatio.contracts.verification import GateResult, GateStatus
from disputatio.verifier.doc_refs import (
    DocRef,
    github_slug,
    iter_headings,
    parse_doc_refs,
    parse_document,
)

CODE_MISSING = "missing"
CODE_ESCAPE = "escape"
CODE_WARNING = "warning"
CODE_LINE_DRIFT = "line_drift"
CODE_SCOPE_ESCAPE = "scope_escape"
CODE_SCOPE_UNPARSED = "scope_unparsed"
CODE_UNRESOLVED_REF = "unresolved_ref"

#: Коды, не роняющие раунд: находка записана, но статус остаётся `pass`.
#: Набор один и тот же для статуса и для строки `reason` — иначе гейт
#: печатал бы «1 fail» на записи, из-за которой сам же не упал.
_WARNING_CODES = frozenset({CODE_WARNING, CODE_UNRESOLVED_REF})

_WARN_ONLY_IF_MISSING = {"code_path"}
_DOC_RELATIVE_KINDS = {"md_link", "autolink"}


def resolve_inside(repo_root: Path, target: str) -> Path | None:
    """Резолвит `target` относительно `repo_root` (репо-относительные виды:
    `declared_existing`/`declared_planned`/`code_path`/`code_line_ref`).

    `None`, если цель выходит за пределы `repo_root` после снятия `..` и
    symlink'ов (`Path.resolve()`) — containment-нарушение (§6). Пустая
    строка (``target == ""``) означает «сам документ» и резолвится в сам
    `repo_root`.
    """
    return _join_and_contain(repo_root, repo_root, target)


def _resolve_relative_to_doc(doc: Path, repo_root: Path, target: str) -> Path | None:
    """Резолвит `md_link`/`autolink` относительно каталога `doc` —
    так их резолвит CommonMark/GitHub, а не относительно `repo_root`.
    Containment по-прежнему проверяется относительно `repo_root`.
    """
    return _join_and_contain(doc.parent, repo_root, target)


def _join_and_contain(base: Path, repo_root: Path, target: str) -> Path | None:
    root = repo_root.resolve()
    if target == "":
        return root
    try:
        candidate = (base / target).resolve()
    except OSError:
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _resolve_ref(
    ref: DocRef, path_text: str, doc: Path, repo_root: Path
) -> Path | None:
    if ref.kind in _DOC_RELATIVE_KINDS:
        return _resolve_relative_to_doc(doc, repo_root, path_text)
    return resolve_inside(repo_root, path_text)


def gate_doc_paths(doc: Path, repo_root: Path) -> GateResult:
    """`fail` на пропавших md_link/autolink/code_line_ref/declared_existing;
    `warning` на уже существующем `declared_planned` и на пропавшем
    `code_path`.
    """
    text = _read_document("doc-paths", doc)
    if isinstance(text, GateResult):
        return text
    refs = parse_doc_refs(text)
    status, entries = _check_paths(refs, doc, repo_root)
    return _build_result("doc-paths", f"internal:doc-paths:{doc}", status, entries)


def gate_doc_links(doc: Path, repo_root: Path) -> GateResult:
    """Разрешимость относительных Markdown-ссылок (`md_link` кроме прочих).

    Reference-ссылка без определения (``[text][ref]``, определения нет) —
    `warning` с кодом `unresolved_ref`, не `fail`: цель не выводима, и
    судить о существовании файла, имени которого документ не назвал, гейт
    не может. Но и молчать не вправе — `doc-links` входит в
    детерминированный критерий сходимости, а `pass` по документу, где
    целая форма прошла мимо проверки, был бы «проверено» про
    непроверенное. Этот вид проверяет только `doc-links`: `doc-paths`
    видит те же формы, и вторая запись о той же ссылке выглядела бы второй
    находкой.
    """
    text = _read_document("doc-links", doc)
    if isinstance(text, GateResult):
        return text
    parsed = parse_document(text)
    refs = [ref for ref in parsed.refs if ref.kind == "md_link"]
    status, entries = _check_paths(refs, doc, repo_root)
    entries += [
        _entry(CODE_UNRESOLVED_REF, item.label, item.line) for item in parsed.unresolved
    ]
    return _build_result("doc-links", f"internal:doc-links:{doc}", status, entries)


def gate_doc_anchors(doc: Path, repo_root: Path) -> GateResult:
    """Существование локальных section anchors — по правилам `github_slug`.

    Отсутствие самого целевого файла — забота `gate_doc_paths`/
    `gate_doc_links`: здесь такой якорь молча пропускается, а не
    дублируется вторым `fail` за ту же причину.
    """
    doc_text = _read_document("doc-anchors", doc)
    if isinstance(doc_text, GateResult):
        return doc_text
    refs = [ref for ref in parse_doc_refs(doc_text) if ref.anchor]
    self_slugs = _slug_set(iter_headings(doc_text))

    entries: list[dict[str, object]] = []
    has_fail = False
    heading_cache: dict[Path, set[str]] = {}
    for ref in refs:
        if ref.target == "":
            slugs = self_slugs
        else:
            resolved = _resolve_relative_to_doc(doc, repo_root, ref.target)
            if resolved is None:
                has_fail = True
                entries.append(_entry(CODE_ESCAPE, ref.target, ref.line))
                continue
            if not resolved.exists():
                continue  # существование пути — забота doc-paths/doc-links
            if resolved not in heading_cache:
                try:
                    target_text = resolved.read_text(encoding="utf-8")
                except OSError:
                    continue  # нечитаемый файл — не про этот якорь
                heading_cache[resolved] = _slug_set(iter_headings(target_text))
            slugs = heading_cache[resolved]
        key = github_slug(ref.anchor or "", {})
        if key not in slugs:
            has_fail = True
            target = f"{ref.target}#{ref.anchor}" if ref.target else f"#{ref.anchor}"
            entries.append(_entry(CODE_MISSING, target, ref.line))

    status = GateStatus.FAIL if has_fail else GateStatus.PASS
    return _build_result("doc-anchors", f"internal:doc-anchors:{doc}", status, entries)


def gate_doc_line_refs(doc: Path, repo_root: Path) -> GateResult:
    """Корректность ссылок `file:line`: путь существует, строка есть.

    При наличии `expected_text` (форма ``` `f.py:42` («текст») ```,
    см. `doc_refs`) содержимое целевой строки обязано совпасть (после
    `strip()` — терпимо к случайным хвостовым пробелам в самой цитате, не к
    расхождению текста); дрейф → `fail` с кодом `line_drift`. Отсутствие
    файла или номер строки за пределами файла — `missing`, выход цели за
    `repo_root` — `escape` (тот же словарь кодов, что и у прочих
    path-гейтов baseline).
    """
    text = _read_document("doc-line-refs", doc)
    if isinstance(text, GateResult):
        return text
    refs = [ref for ref in parse_doc_refs(text) if ref.kind == "code_line_ref"]
    entries: list[dict[str, object]] = []
    has_fail = False
    for ref in refs:
        path_text, _, line_str = ref.target.rpartition(":")
        line_no = int(line_str)
        resolved = resolve_inside(repo_root, path_text)
        if resolved is None:
            has_fail = True
            entries.append(_entry(CODE_ESCAPE, ref.target, ref.line))
            continue
        try:
            target_lines = resolved.read_text(encoding="utf-8").splitlines()
        except OSError:
            has_fail = True
            entries.append(_entry(CODE_MISSING, path_text, ref.line))
            continue
        if line_no < 1 or line_no > len(target_lines):
            has_fail = True
            entries.append(_entry(CODE_MISSING, ref.target, ref.line))
            continue
        if ref.expected_text is None:
            continue
        actual = target_lines[line_no - 1]
        if actual.strip() != ref.expected_text.strip():
            has_fail = True
            entries.append(_entry(CODE_LINE_DRIFT, ref.target, ref.line))
    status = GateStatus.FAIL if has_fail else GateStatus.PASS
    return _build_result(
        "doc-line-refs", f"internal:doc-line-refs:{doc}", status, entries
    )


_DIFF_MINUS_RE = re.compile(r"^--- (?:a/(?P<path>.+)|/dev/null)$")
_DIFF_PLUS_RE = re.compile(r"^\+\+\+ (?:b/(?P<path>.+)|/dev/null)$")
_RENAME_FROM_RE = re.compile(r"^rename from (?P<path>.+)$")
_RENAME_TO_RE = re.compile(r"^rename to (?P<path>.+)$")
_DIFF_PATH_PATTERNS = (
    _DIFF_MINUS_RE,
    _DIFF_PLUS_RE,
    _RENAME_FROM_RE,
    _RENAME_TO_RE,
)
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+\d+(?:,(?P<new>\d+))? @@")
_DIFF_GIT_PREFIX = "diff --git "


def _iter_file_metadata_lines(patch: str) -> Iterator[tuple[int, str]]:
    """Строки МЕТАДАННЫХ файлов unified diff — всё, что вне hunk'ов.

    Патч — не плоский список строк, и разбор regex'ом по всем подряд
    путает содержимое с заголовками: строка документа ``-- a/other.py``
    под удалением печатается как ``--- a/other.py``, а ``++ b/other.py``
    под добавлением — как ``+++ b/other.py``. Обе неотличимы от
    заголовка файла ИМЕННО В ОТРЫВЕ от структуры, а в структуре
    неоднозначности нет: заголовок файла может стоять только до первого
    ``@@``, а внутри hunk'а каждая строка несёт префикс и посчитана в его
    заголовке. Документы этого пайплайна показывают unified diff в
    примерах — то есть форма не экзотическая, а штатная.

    Счётчик строк hunk'а ведётся по обеим сторонам: ``@@ -a,N +b,M @@``
    (без числа — 1, как в git'е). ``\\ No newline at end of file`` —
    примечание, а не строка содержимого, и в счёт не идёт. Строка, не
    несущая ни одного из префиксов ``' '``/``'-'``/``'+'``/``'\\'`` при
    незакрытом счётчике, обрывает hunk и разбирается как метаданные:
    оборванный патч не должен уводить остаток разбора в слепоту.
    """
    remaining_old = 0
    remaining_new = 0
    for lineno, raw_line in enumerate(patch.splitlines(), start=1):
        if remaining_old > 0 or remaining_new > 0:
            marker = raw_line[:1]
            if marker == "\\":
                continue
            if marker in (" ", ""):
                remaining_old = max(0, remaining_old - 1)
                remaining_new = max(0, remaining_new - 1)
                continue
            if marker == "-":
                remaining_old = max(0, remaining_old - 1)
                continue
            if marker == "+":
                remaining_new = max(0, remaining_new - 1)
                continue
            remaining_old = remaining_new = 0
        hunk = _HUNK_HEADER_RE.match(raw_line)
        if hunk is not None:
            remaining_old = int(hunk.group("old") or 1)
            remaining_new = int(hunk.group("new") or 1)
            continue
        yield lineno, raw_line


@dataclass(frozen=True, slots=True)
class _FileSection:
    """Секция одного файла в патче: строка `diff --git` и её метаданные.

    `header_rest` — хвост строки `diff --git ` (то есть `a/OLD b/NEW`) или
    `None` у преамбулы: строк до первой `diff --git` в выводе git'а не
    бывает, но патч приходит из вывода агента, а не из доказательства.
    """

    header_line: int
    header_rest: str | None
    lines: tuple[tuple[int, str], ...]


def _iter_file_sections(patch: str) -> Iterator[_FileSection]:
    """Метаданные патча, разложенные по файлам границей `diff --git`.

    Секция — единица учёта `doc-scope`: путь, названный текстовыми
    заголовками ОДНОГО файла, ничего не говорит о соседнем. Разбор плоским
    списком строк этого различия не знал и считал непустой список путей по
    всему патчу достаточным — то есть правка разрешённого документа
    прикрывала соседнюю секцию, у которой заголовков нет вовсе.
    """
    header_line = 0
    header_rest: str | None = None
    body: list[tuple[int, str]] = []
    for lineno, raw_line in _iter_file_metadata_lines(patch):
        if not raw_line.startswith(_DIFF_GIT_PREFIX):
            body.append((lineno, raw_line))
            continue
        if header_rest is not None or body:
            yield _FileSection(header_line, header_rest, tuple(body))
        header_line = lineno
        header_rest = raw_line[len(_DIFF_GIT_PREFIX) :]
        body = []
    if header_rest is not None or body:
        yield _FileSection(header_line, header_rest, tuple(body))


def _paths_from_diff_git(rest: str) -> tuple[str, ...] | None:
    """Пути из `diff --git a/OLD b/NEW`; `None` — форма неоднозначна.

    Разбирается только как **запасной** источник: у секции без текстовых
    заголовков (бинарное изменение, смена режима, создание пустого файла)
    другого источника пути нет, а молчание о такой секции — ровно тот
    fail-open, ради которого функция написана. Пробел-разделитель в общем
    случае не отличим от пробела в имени файла, поэтому догадка не
    допускается: совпадение сторон (обычная правка) снимает
    неоднозначность, единственный кандидат — тоже, а всё прочее возвращает
    `None`, и вызывающий обязан записать `scope_unparsed`.
    """
    if not rest.startswith("a/"):
        return None
    pairs = [
        (rest[2:idx], rest[idx + 3 :])
        for idx in range(2, len(rest))
        if rest[idx] == " " and rest.startswith("b/", idx + 1)
    ]
    pairs = [(old, new) for old, new in pairs if old and new]
    identical = [old for old, new in pairs if old == new]
    if identical:
        return (identical[0],)
    if len(pairs) == 1:
        return pairs[0]
    return None


def gate_doc_scope(patch: str, allowed: tuple[str, ...]) -> GateResult:
    """Диф раунда трогает только пути из `allowed` (§6, doc-scope).

    Пути читаются из заголовков unified diff — `--- a/<path>`/`+++ b/<path>`
    (`/dev/null` на любой стороне — сторона создания/удаления файла, не
    путь) — и из `rename from <path>`/`rename to <path>`: **чистое**
    переименование (`git mv` без правки содержимого) печатает ТОЛЬКО пару
    `rename from`/`rename to`, без `---`/`+++` вовсе — пропуск этой формы
    открыл бы обход всего baseline одной командой (фикс-раунд 1, Critical:
    ревьюер воспроизвёл живым `git mv`, четыре content-гейта уходят в
    `skip`, потому что документ по старому пути исчезает, а `doc-scope` без
    этой формы молчал). Эта форма однозначна и не требует разбора
    неоднозначной `diff --git a/… b/…` строки с пробелом-разделителем,
    который в общем случае не отличим от пробела в самом имени файла.

    Путь фиксируется по **первому** заголовку, где он встретился: обычная
    правка называет один и тот же путь и в `---`, и в `+++` — одна запись,
    не две; переименование с одновременной правкой содержимого печатает обе
    формы для разных путей (`rename from`/`to` и следом `---`/`+++` с теми
    же двумя путями) — дедуп по пути на весь патч не даёт задвоения записи
    об одном и том же файле. Каждый непустой путь вне `allowed` даёт
    `scope_escape` — граница контура, а не список для галочки.

    Заголовки берутся **только из метаданных файлов**
    (`_iter_file_metadata_lines`): строки внутри hunk'ов — содержимое, и
    строка документа, показывающая unified diff, под удалением/добавлением
    выглядит как заголовок в точности. Разбор без состояния читал бы её
    как метаданные и ронял весь `VerificationReport` на файле, которого
    патч не трогал.

    **Учёт ведётся по секциям файлов, и секция без единого выведенного пути
    — не успех, а находка** (A1). Текстовых заголовков нет у целого класса
    изменений: бинарное (`Binary files … differ`, `GIT binary patch`),
    смена режима, создание пустого файла. Общий счёт путей по всему патчу
    их прикрывал: правка разрешённой спеки давала непустой список, и
    соседняя секция с запрещённым `other.bin` проходила границу молча.
    Путь такой секции выводится из её `diff --git` (`_paths_from_diff_git`);
    не вывелся — `scope_unparsed` и `fail`, потому что «не разобрал» и «не
    вышел за границу» — разные утверждения, и гейт вправе делать только
    второе.
    """
    allowed_set = set(allowed)
    first_seen: dict[str, int] = {}
    findings: list[tuple[int, str, str]] = []
    for section in _iter_file_sections(patch):
        named = False
        for lineno, raw_line in section.lines:
            for pattern in _DIFF_PATH_PATTERNS:
                match = pattern.match(raw_line)
                if match is None:
                    continue
                path = match.group("path")
                if path is not None:
                    named = True
                    first_seen.setdefault(path, lineno)
                break
        if named or section.header_rest is None:
            continue
        derived = _paths_from_diff_git(section.header_rest)
        if derived is None:
            findings.append(
                (section.header_line, CODE_SCOPE_UNPARSED, section.header_rest)
            )
            continue
        for path in derived:
            first_seen.setdefault(path, section.header_line)
    findings.extend(
        (lineno, CODE_SCOPE_ESCAPE, path)
        for path, lineno in first_seen.items()
        if path not in allowed_set
    )
    unparsed = _unparsed_patch(patch) if not first_seen and not findings else None
    if unparsed is not None:
        findings.append(unparsed)
    entries = [
        _entry(code, target, lineno) for lineno, code, target in sorted(findings)
    ]
    status = GateStatus.FAIL if entries else GateStatus.PASS
    return _build_result("doc-scope", "internal:doc-scope", status, entries)


def _unparsed_patch(patch: str) -> tuple[int, str, str] | None:
    """Находка «патч непуст, а разобрать в нём нечего»; `None` — патч пуст.

    Пустой патч — законный результат раунда (`analyze`-правка, не тронувшая
    файлов), и `PASS` по нему честен. Непустой патч, из которого не выведено
    ни одного пути и ни одной секции, честным `PASS` не является: гейт не
    знает, что именно там описано.
    """
    for lineno, raw_line in enumerate(patch.splitlines(), start=1):
        if raw_line.strip():
            return (lineno, CODE_SCOPE_UNPARSED, raw_line)
    return None


def _slug_set(headings: list[tuple[int, str]]) -> set[str]:
    seen: dict[str, int] = {}
    return {github_slug(text, seen) for _, text in headings}


def _path_for_existence(ref: DocRef) -> str:
    """Путь без суффикса `:LINE` для `code_line_ref`."""
    if ref.kind == "code_line_ref":
        path, _, _ = ref.target.rpartition(":")
        return path
    return ref.target


def _entry(code: str, target: str, line: int) -> dict[str, object]:
    return {"code": code, "target": target, "line": line}


def _read_document(name: str, doc: Path) -> str | GateResult:
    """Читает `doc`; несуществующий/нечитаемый документ — `skip`, не исключение.

    Конвенция — как у `runner.run_gate`: сбой самого запуска (здесь —
    чтения) превращается в строку отчёта, а не летит наружу.
    """
    try:
        return doc.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _skipped(name, doc, f"document not found: {doc}")
    except OSError as exc:
        return _skipped(name, doc, f"cannot read document: {exc}")


def _skipped(name: str, doc: Path, reason: str) -> GateResult:
    return GateResult(
        name=name,
        cmd=f"internal:{name}:{doc}",
        status=GateStatus.SKIP,
        reason=reason,
    )


def _check_paths(
    refs: list[DocRef], doc: Path, repo_root: Path
) -> tuple[GateStatus, list[dict[str, object]]]:
    entries: list[dict[str, object]] = []
    has_fail = False
    for ref in refs:
        path_text = _path_for_existence(ref)
        if not path_text:
            continue  # чистый якорь без пути — не про существование файла
        resolved = _resolve_ref(ref, path_text, doc, repo_root)
        if resolved is None:
            has_fail = True
            entries.append(_entry(CODE_ESCAPE, path_text, ref.line))
            continue
        exists = resolved.exists()
        if ref.kind == "declared_planned":
            if exists:
                entries.append(_entry(CODE_WARNING, path_text, ref.line))
            continue
        if exists:
            continue
        if ref.kind in _WARN_ONLY_IF_MISSING:
            entries.append(_entry(CODE_WARNING, path_text, ref.line))
        else:
            has_fail = True
            entries.append(_entry(CODE_MISSING, path_text, ref.line))
    status = GateStatus.FAIL if has_fail else GateStatus.PASS
    return status, entries


def _build_result(
    name: str, cmd: str, status: GateStatus, entries: list[dict[str, object]]
) -> GateResult:
    tail = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)
    fails = sum(1 for entry in entries if entry["code"] not in _WARNING_CODES)
    warnings = sum(1 for entry in entries if entry["code"] in _WARNING_CODES)
    parts = []
    if fails:
        parts.append(f"{fails} fail")
    if warnings:
        parts.append(f"{warnings} warning")
    reason = ", ".join(parts) if parts else None
    return GateResult(
        name=name,
        cmd=cmd,
        status=status,
        exit_code=0 if status is GateStatus.PASS else 1,
        tail=tail,
        reason=reason,
    )
