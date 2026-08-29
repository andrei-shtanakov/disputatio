"""Тесты doc-гейтов 1-3: `doc-paths`/`doc-links`/`doc-anchors` (TASK-007).

Fixture-репо — обычный `tmp_path`: гейты работают по файловой системе, без
git (`doc-scope` — другой гейт, вне задачи 7). Импорт `doc_gates` — внутри
тестов, конвенция red-фазы (см. `test_doc_refs.py`).
"""

import json
from pathlib import Path
from types import ModuleType

from disputatio.contracts.verification import GateStatus


def _import_doc_gates() -> ModuleType:
    try:
        from disputatio.verifier import doc_gates
    except ImportError as exc:  # red-фаза: doc_gates.py ещё не создан
        raise AssertionError(
            "src/disputatio/verifier/doc_gates.py ещё не создан"
        ) from exc
    return doc_gates


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tail_entries(tail: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in tail.splitlines() if line]


# ---------------------------------------------------------------------------
# gate_doc_paths — семантика утверждений (Modify:/Test:/Create:)
# ---------------------------------------------------------------------------


def test_modify_nonexistent_path_fails(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "plan.md", "- Modify: `missing.py`\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "missing", "target": "missing.py", "line": 1}]


def test_create_nonexistent_path_passes_declaration_of_intent(tmp_path: Path) -> None:
    """Кейс-эталон §6: спека/план вправе объявлять ненаписанный модуль."""
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "plan.md",
        "- Create: `src/disputatio/runtime/pipeline_runner.py`\n",
    )

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_create_already_existing_path_is_a_warning_not_a_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "existing.py").write_text("x = 1\n", encoding="utf-8")
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "plan.md", "- Create: `existing.py`\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.PASS
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "warning", "target": "existing.py", "line": 1}]


def test_inline_code_path_missing_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "spec.md",
        "Модуль `src/disputatio/runtime/pipeline_runner.py` ещё не написан.\n",
    )

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.PASS
    entries = _tail_entries(result.tail)
    assert entries == [
        {
            "code": "warning",
            "target": "src/disputatio/runtime/pipeline_runner.py",
            "line": 1,
        }
    ]


def test_markdown_link_to_missing_target_fails(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "spec.md", "[план](docs/plans/missing.md)\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [
        {"code": "missing", "target": "docs/plans/missing.md", "line": 1}
    ]


def test_markdown_link_to_existing_target_passes(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "plans" / "foo.md", "# Foo\n")
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "spec.md", "[план](docs/plans/foo.md)\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_no_recognized_forms_passes_trivially(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "spec.md", "Модуль src/disputatio/foo.py упомянут в прозе.\n"
    )

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert result.tail == ""
    assert result.reason is None


# ---------------------------------------------------------------------------
# Containment — resolve_inside / symlink-escape
# ---------------------------------------------------------------------------


def test_resolve_inside_rejects_dot_dot_escape(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()

    assert doc_gates.resolve_inside(tmp_path, "../outside.py") is None


def test_resolve_inside_accepts_path_within_root(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    (tmp_path / "inside.py").write_text("x = 1\n", encoding="utf-8")

    resolved = doc_gates.resolve_inside(tmp_path, "inside.py")

    assert resolved == (tmp_path / "inside.py").resolve()


def test_dot_dot_path_in_declared_existing_fails_with_escape_code(
    tmp_path: Path,
) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "plan.md", "- Modify: `../outside.py`\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "escape", "target": "../outside.py", "line": 1}]


def test_symlink_escaping_repo_root_fails_with_escape_code(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.py").write_text("x = 1\n", encoding="utf-8")
    (repo_root / "link_out").symlink_to(outside, target_is_directory=True)
    doc_gates = _import_doc_gates()
    doc = _write(repo_root / "plan.md", "- Modify: `link_out/real.py`\n")

    result = doc_gates.gate_doc_paths(doc, repo_root)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "escape", "target": "link_out/real.py", "line": 1}]


# ---------------------------------------------------------------------------
# md_link/autolink — база резолвинга: каталог документа, не repo_root
# (фикс-раунд 1: CommonMark/GitHub резолвят относительные ссылки от
# каталога документа, а не от корня репозитория).
# ---------------------------------------------------------------------------


def test_relative_markdown_link_resolves_against_document_directory(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "design.md", "# Design\n")
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "docs" / "plans" / "plan.md", "[design](../design.md)\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_relative_markdown_link_escaping_above_repo_root_fails(
    tmp_path: Path,
) -> None:
    doc_gates = _import_doc_gates()
    # doc.parent = tmp_path/docs; ".." -> tmp_path (repo_root), ".." ещё раз
    # -> выше repo_root.
    doc = _write(tmp_path / "docs" / "plan.md", "[outside](../../outside.md)\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "escape", "target": "../../outside.md", "line": 1}]


def test_autolink_resolves_against_document_directory(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "design.md", "# Design\n")
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "docs" / "plans" / "plan.md", "<../design.md>\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


# ---------------------------------------------------------------------------
# gate_doc_links — только md_link, независимо от прочих гейтов
# ---------------------------------------------------------------------------


def test_gate_doc_links_ignores_non_link_kinds(tmp_path: Path) -> None:
    """Битый Modify: не должен ронять doc-links — это забота doc-paths."""
    _write(tmp_path / "docs" / "plans" / "foo.md", "# Foo\n")
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "plan.md",
        "- Modify: `missing.py`\n[план](docs/plans/foo.md)\n",
    )

    result = doc_gates.gate_doc_links(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_gate_doc_links_fails_on_broken_relative_link(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "spec.md", "[план](docs/plans/missing.md)\n")

    result = doc_gates.gate_doc_links(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [
        {"code": "missing", "target": "docs/plans/missing.md", "line": 1}
    ]


def test_gate_doc_links_resolves_reference_style_link(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "plans" / "foo.md", "# Foo\n")
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "spec.md",
        "Смотри [план][plan-ref].\n\n[plan-ref]: docs/plans/foo.md\n",
    )

    result = doc_gates.gate_doc_links(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


# ---------------------------------------------------------------------------
# gate_doc_anchors — существование section anchors
# ---------------------------------------------------------------------------


def test_same_document_anchor_exists_passes(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "spec.md",
        "## Overview\n\n[выше](#overview)\n",
    )

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_same_document_missing_anchor_fails(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "spec.md",
        "## Overview\n\n[выше](#no-such-section)\n",
    )

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "missing", "target": "#no-such-section", "line": 3}]


def test_cross_document_anchor_is_checked_against_target_headings(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "other.md", "## Section One\n")
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "spec.md", "[раздел](other.md#section-one)\n")

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_cross_document_missing_anchor_fails(tmp_path: Path) -> None:
    _write(tmp_path / "other.md", "## Section One\n")
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "spec.md", "[раздел](other.md#nope)\n")

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "missing", "target": "other.md#nope", "line": 1}]


def test_anchor_into_missing_target_document_is_skipped(tmp_path: Path) -> None:
    """Отсутствие самого файла — забота doc-paths/doc-links, не doc-anchors."""
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "spec.md", "[раздел](missing.md#nope)\n")

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_duplicate_headings_get_numeric_suffix_for_anchor_matching(
    tmp_path: Path,
) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "spec.md",
        "## Overview\n\n## Overview\n\n[второй](#overview-1)\n",
    )

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_percent_encoded_anchor_fragment_matches_heading(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "spec.md",
        "## Some Heading\n\n[ссылка](#some%20heading)\n",
    )

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


# ---------------------------------------------------------------------------
# gate_doc_paths — все fail-формы пропавшего пути закреплены гейт-тестом,
# не только парсер-тестом на распознавание (фикс-раунд 1, Important).
# ---------------------------------------------------------------------------


def test_autolink_to_missing_target_fails(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(tmp_path / "spec.md", "См. <src/disputatio/missing.py> целиком.\n")

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [
        {"code": "missing", "target": "src/disputatio/missing.py", "line": 1}
    ]


def test_code_line_ref_to_missing_target_fails(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    doc = _write(
        tmp_path / "spec.md",
        "См. `src/disputatio/missing.py:42` за реализацией.\n",
    )

    result = doc_gates.gate_doc_paths(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    # Отчёт несёт путь без суффикса `:42` — сама целевая строка не про
    # существование файла (см. `_path_for_existence`).
    assert entries == [
        {"code": "missing", "target": "src/disputatio/missing.py", "line": 1}
    ]


# ---------------------------------------------------------------------------
# Отсутствующий/нечитаемый документ — skip гейта, не исключение наружу
# (фикс-раунд 1, Important; конвенция — runner.run_gate).
# ---------------------------------------------------------------------------


def test_gate_doc_paths_skips_on_missing_document(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    missing = tmp_path / "does-not-exist.md"

    result = doc_gates.gate_doc_paths(missing, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason


def test_gate_doc_links_skips_on_missing_document(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    missing = tmp_path / "does-not-exist.md"

    result = doc_gates.gate_doc_links(missing, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason


def test_gate_doc_anchors_skips_on_missing_document(tmp_path: Path) -> None:
    doc_gates = _import_doc_gates()
    missing = tmp_path / "does-not-exist.md"

    result = doc_gates.gate_doc_anchors(missing, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason


def test_gate_doc_paths_skips_on_unreadable_document(tmp_path: Path) -> None:
    """Директория на месте документа — тот же класс сбоя, что и отсутствие."""
    doc_gates = _import_doc_gates()
    directory_as_doc = tmp_path / "looks-like-a-doc.md"
    directory_as_doc.mkdir()

    result = doc_gates.gate_doc_paths(directory_as_doc, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason
