"""Doc-гейты 1-3: `doc-paths`/`doc-links`/`doc-anchors` (SPEC-002 §6).

Три детерминированных гейта поверх `parse_doc_refs` (`doc_refs.py`), без
запуска внешних процессов — статус вычисляется напрямую из файловой
системы. `doc-line-refs` и `doc-scope` (два оставшихся гейта baseline §6)
— другие задачи, здесь не реализуются.

**Утверждение о существовании отличается от объявления намерения** (§6):
`gate_doc_paths` роняет раунд только на формах, которые утверждают
существование (`md_link`, `autolink`, `code_line_ref`, `declared_existing`).
`declared_planned` (путь после ``Create:``) — объявление намерения:
отсутствие — норма, существование — `warning` (задача объявляет создание
уже существующего). Прочий `code_path` при отсутствии — тоже `warning`, не
`fail`: спека, проектирующая ещё не написанный модуль, иначе не сошлась бы
никогда.
"""

from __future__ import annotations

import json
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

_WARN_ONLY_IF_MISSING = {"code_path"}


def resolve_inside(repo_root: Path, target: str) -> Path | None:
    """Резолвит `target` относительно `repo_root`.

    `None`, если цель выходит за пределы `repo_root` после снятия `..` и
    symlink'ов (`Path.resolve()`) — containment-нарушение (§6). Пустая
    строка (``target == ""``) означает «сам документ» (якорь без пути) и
    резолвится в сам `repo_root`.
    """
    root = repo_root.resolve()
    if target == "":
        return root
    try:
        candidate = (repo_root / target).resolve()
    except OSError:
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def gate_doc_paths(doc: Path, repo_root: Path) -> GateResult:
    """`fail` на пропавших md_link/autolink/code_line_ref/declared_existing;
    `warning` на уже существующем `declared_planned` и на пропавшем
    `code_path`.
    """
    refs = parse_doc_refs(doc.read_text(encoding="utf-8"))
    status, entries = _check_paths(refs, repo_root)
    return _build_result("doc-paths", doc, status, entries)


def gate_doc_links(doc: Path, repo_root: Path) -> GateResult:
    """Разрешимость относительных Markdown-ссылок (`md_link` кроме прочих)."""
    refs = [
        ref
        for ref in parse_doc_refs(doc.read_text(encoding="utf-8"))
        if ref.kind == "md_link"
    ]
    status, entries = _check_paths(refs, repo_root)
    return _build_result("doc-links", doc, status, entries)


def gate_doc_anchors(doc: Path, repo_root: Path) -> GateResult:
    """Существование локальных section anchors — по правилам `github_slug`.

    Отсутствие самого целевого файла — забота `gate_doc_paths`/
    `gate_doc_links`: здесь такой якорь молча пропускается, а не
    дублируется вторым `fail` за ту же причину.
    """
    doc_text = doc.read_text(encoding="utf-8")
    refs = [ref for ref in parse_doc_refs(doc_text) if ref.anchor]
    self_slugs = _slug_set(iter_headings(doc_text))

    entries: list[dict[str, object]] = []
    has_fail = False
    heading_cache: dict[Path, set[str]] = {}
    for ref in refs:
        if ref.target == "":
            slugs = self_slugs
        else:
            resolved = resolve_inside(repo_root, ref.target)
            if resolved is None:
                has_fail = True
                entries.append(_entry(CODE_ESCAPE, ref.target, ref.line))
                continue
            if not resolved.exists():
                continue  # существование пути — забота doc-paths/doc-links
            if resolved not in heading_cache:
                heading_cache[resolved] = _slug_set(
                    iter_headings(resolved.read_text(encoding="utf-8"))
                )
            slugs = heading_cache[resolved]
        key = github_slug(ref.anchor or "", {})
        if key not in slugs:
            has_fail = True
            target = f"{ref.target}#{ref.anchor}" if ref.target else f"#{ref.anchor}"
            entries.append(_entry(CODE_MISSING, target, ref.line))

    status = GateStatus.FAIL if has_fail else GateStatus.PASS
    return _build_result("doc-anchors", doc, status, entries)


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


def _check_paths(
    refs: list[DocRef], repo_root: Path
) -> tuple[GateStatus, list[dict[str, object]]]:
    entries: list[dict[str, object]] = []
    has_fail = False
    for ref in refs:
        path_text = _path_for_existence(ref)
        if not path_text:
            continue  # чистый якорь без пути — не про существование файла
        resolved = resolve_inside(repo_root, path_text)
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
    name: str, doc: Path, status: GateStatus, entries: list[dict[str, object]]
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
        cmd=f"internal:{name}:{doc}",
        status=status,
        exit_code=0 if status is GateStatus.PASS else 1,
        tail=tail,
        reason=reason,
    )
