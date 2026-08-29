"""Тесты гейта `doc-line-refs` (TASK-008, §6 SPEC-002).

`file:line` — существование пути и номера строки; при наличии якорного
текста в форме `` `file.py:42` («текст строки») `` (см. `doc_refs`) —
совпадение содержимого строки, дрейф → `fail` с кодом `line_drift`.
"""

import json
from pathlib import Path

from disputatio.contracts.verification import GateStatus
from disputatio.verifier import doc_gates


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tail_entries(tail: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in tail.splitlines() if line]


def test_line_ref_without_expected_text_passes_when_line_exists(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "src" / "mod.py", "a = 1\nb = 2\nc = 3\n")
    doc = _write(tmp_path / "spec.md", "См. `src/mod.py:2` за деталями.\n")

    result = doc_gates.gate_doc_line_refs(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_line_ref_with_matching_expected_text_passes(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "mod.py", "a = 1\nb = 2\nc = 3\n")
    doc = _write(tmp_path / "spec.md", "См. `src/mod.py:2` («b = 2»).\n")

    result = doc_gates.gate_doc_line_refs(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_line_ref_with_drifted_expected_text_fails_with_line_drift(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "src" / "mod.py", "a = 1\nb = 999\nc = 3\n")
    doc = _write(tmp_path / "spec.md", "См. `src/mod.py:2` («b = 2»).\n")

    result = doc_gates.gate_doc_line_refs(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "line_drift", "target": "src/mod.py:2", "line": 1}]


def test_line_ref_to_missing_file_fails(tmp_path: Path) -> None:
    doc = _write(tmp_path / "spec.md", "См. `src/missing.py:2` за деталями.\n")

    result = doc_gates.gate_doc_line_refs(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "missing", "target": "src/missing.py", "line": 1}]


def test_line_ref_past_end_of_file_fails(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "mod.py", "a = 1\nb = 2\n")
    doc = _write(tmp_path / "spec.md", "См. `src/mod.py:99` за деталями.\n")

    result = doc_gates.gate_doc_line_refs(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "missing", "target": "src/mod.py:99", "line": 1}]


def test_line_ref_escaping_repo_root_fails_with_escape_code(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    doc = _write(repo_root / "spec.md", "См. `../outside.py:1` за деталями.\n")

    result = doc_gates.gate_doc_line_refs(doc, repo_root)

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "escape", "target": "../outside.py:1", "line": 1}]


def test_gate_doc_line_refs_skips_on_missing_document(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"

    result = doc_gates.gate_doc_line_refs(missing, tmp_path)

    assert result.status is GateStatus.SKIP
    assert result.exit_code is None
    assert result.reason


def test_gate_doc_line_refs_ignores_non_line_ref_kinds(tmp_path: Path) -> None:
    """`code_path`/`md_link` не должны попадать в `doc-line-refs`."""
    _write(tmp_path / "docs" / "plans" / "foo.md", "# Foo\n")
    doc = _write(
        tmp_path / "spec.md",
        "`src/disputatio/foo.py` и [план](docs/plans/foo.md)\n",
    )

    result = doc_gates.gate_doc_line_refs(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


# ---------------------------------------------------------------------------
# gate_doc_scope — граница контура: диф раунда трогает только allowed
# ---------------------------------------------------------------------------


def test_scope_patch_touching_only_allowed_path_passes() -> None:
    patch = (
        "diff --git a/spec.md b/spec.md\n"
        "--- a/spec.md\n"
        "+++ b/spec.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_scope_patch_touching_disallowed_path_fails() -> None:
    patch = (
        "diff --git a/spec.md b/spec.md\n"
        "--- a/spec.md\n"
        "+++ b/spec.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/other.py b/other.py\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )

    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "scope_escape", "target": "other.py", "line": 9}]


def test_scope_empty_patch_passes() -> None:
    result = doc_gates.gate_doc_scope("", ("spec.md",))

    assert result.status is GateStatus.PASS
    assert _tail_entries(result.tail) == []


def test_scope_new_file_outside_allowed_fails() -> None:
    patch = (
        "diff --git a/other.py b/other.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/other.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )

    result = doc_gates.gate_doc_scope(patch, ("spec.md",))

    assert result.status is GateStatus.FAIL
    entries = _tail_entries(result.tail)
    assert entries == [{"code": "scope_escape", "target": "other.py", "line": 4}]
