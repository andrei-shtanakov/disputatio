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
from pathlib import Path

from disputatio.contracts.verification import GateResult, GateStatus
from disputatio.verifier.doc_refs import (
    DocRef,
    github_slug,
    iter_headings,
    parse_doc_refs,
)

CODE_MISSING = "missing"
CODE_ESCAPE = "escape"
CODE_WARNING = "warning"
CODE_LINE_DRIFT = "line_drift"
CODE_SCOPE_ESCAPE = "scope_escape"

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
    """Разрешимость относительных Markdown-ссылок (`md_link` кроме прочих)."""
    text = _read_document("doc-links", doc)
    if isinstance(text, GateResult):
        return text
    refs = [ref for ref in parse_doc_refs(text) if ref.kind == "md_link"]
    status, entries = _check_paths(refs, doc, repo_root)
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


def gate_doc_scope(patch: str, allowed: tuple[str, ...]) -> GateResult:
    """Диф раунда трогает только пути из `allowed` (§6, doc-scope).

    Пути читаются из парных заголовков unified diff — `--- a/<path>`
    сразу за ней `+++ b/<path>` (`/dev/null` на любой стороне — сторона
    создания/удаления файла, не путь): эта форма однозначна и не требует
    разбора неоднозначной `diff --git a/… b/…` строки с пробелом-
    разделителем, который в общем случае не отличим от пробела в самом
    имени файла. Обычная правка называет один и тот же путь в обеих строках
    заголовка — он даёт ровно одну запись, не две; переименование называет
    разные пути на `---`/`+++` — проверяются оба, каждый в `allowed` или нет
    сам по себе. Каждый непустой путь вне `allowed` даёт `scope_escape` —
    граница контура, а не список для галочки.
    """
    allowed_set = set(allowed)
    entries: list[dict[str, object]] = []
    lines = patch.splitlines()
    index = 0
    total = len(lines)
    while index < total:
        minus_match = _DIFF_MINUS_RE.match(lines[index])
        if minus_match is None:
            index += 1
            continue
        plus_match = (
            _DIFF_PLUS_RE.match(lines[index + 1]) if index + 1 < total else None
        )
        lineno = index + 2  # 1-based номер строки `+++` (или следующей за `---`)
        paths = {
            path
            for path in (
                minus_match.group("path"),
                plus_match.group("path") if plus_match else None,
            )
            if path is not None
        }
        for path in sorted(paths):
            if path not in allowed_set:
                entries.append(_entry(CODE_SCOPE_ESCAPE, path, lineno))
        index += 2 if plus_match else 1
    status = GateStatus.FAIL if entries else GateStatus.PASS
    return _build_result("doc-scope", "internal:doc-scope", status, entries)


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
    fails = sum(1 for entry in entries if entry["code"] != CODE_WARNING)
    warnings = sum(1 for entry in entries if entry["code"] == CODE_WARNING)
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
