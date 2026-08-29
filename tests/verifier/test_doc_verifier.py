"""Тесты `DocVerifier` — реализация порта `Verifier` для doc-контуров
(TASK-008, §6 SPEC-002).

Baseline — пять doc-гейтов, гоняются на каждом `verify()` без возможности
отключения: у конструктора нет параметра, которым это можно было бы сделать
(§6, «Флага unsafe-отключения в v1 нет»). `tmp_git_repo` — фикстура
`tests/verifier/conftest.py`.
"""

import inspect
from pathlib import Path

from disputatio.contracts.ports import Verifier
from disputatio.contracts.verification import GateStatus, OverallStatus
from disputatio.verifier import GateSpec
from disputatio.verifier.doc_verifier import DocVerifier

_BASELINE_NAMES = {
    "doc-paths",
    "doc-links",
    "doc-anchors",
    "doc-line-refs",
    "doc-scope",
}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_doc_verifier_satisfies_verifier_port(tmp_git_repo: Path) -> None:
    spec = _write(tmp_git_repo / "spec.md", "# Spec\n")

    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
    )

    assert isinstance(verifier, Verifier)


def test_baseline_gate_names_are_always_present_and_clean_doc_passes(
    tmp_git_repo: Path,
) -> None:
    spec = _write(tmp_git_repo / "spec.md", "# Spec\n")
    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
    )

    report = verifier.verify(1)

    names = {gate.name for gate in report.gates}
    assert _BASELINE_NAMES <= names
    assert report.overall is OverallStatus.PASS
    assert report.round == 1


def test_extra_gates_are_appended_after_baseline(tmp_git_repo: Path) -> None:
    spec = _write(tmp_git_repo / "spec.md", "# Spec\n")
    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
        extra=[GateSpec(name="lint", cmd="true")],
    )

    report = verifier.verify(1)

    names = [gate.name for gate in report.gates]
    assert names[-1] == "lint"
    assert report.gates[-1].status is GateStatus.PASS
    assert _BASELINE_NAMES <= set(names)


def test_extra_empty_by_default_still_runs_full_baseline(tmp_git_repo: Path) -> None:
    spec = _write(tmp_git_repo / "spec.md", "# Spec\n")
    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
    )

    report = verifier.verify(1)

    names = [gate.name for gate in report.gates]
    assert set(names) == _BASELINE_NAMES


def test_overall_fails_when_a_baseline_doc_gate_fails(tmp_git_repo: Path) -> None:
    spec = _write(tmp_git_repo / "spec.md", "[план](docs/plans/missing.md)\n")
    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
    )

    report = verifier.verify(1)

    assert report.overall is OverallStatus.FAIL


def test_overall_fails_on_scope_escape(tmp_git_repo: Path) -> None:
    spec = _write(tmp_git_repo / "spec.md", "# Spec\n")
    patch = (
        "diff --git a/spec.md b/spec.md\n"
        "--- a/spec.md\n"
        "+++ b/spec.md\n"
        "@@ -1 +1 @@\n"
        "-# Spec\n"
        "+# Spec!\n"
        "diff --git a/other.py b/other.py\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: patch,
    )

    report = verifier.verify(1)

    assert report.overall is OverallStatus.FAIL
    scope_gate = next(g for g in report.gates if g.name == "doc-scope")
    assert scope_gate.status is GateStatus.FAIL


def test_patch_reader_receives_round_number(tmp_git_repo: Path) -> None:
    spec = _write(tmp_git_repo / "spec.md", "# Spec\n")
    seen_rounds: list[int] = []

    def _patch_reader(round_no: int) -> str:
        seen_rounds.append(round_no)
        return ""

    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=_patch_reader,
    )

    verifier.verify(3)

    assert seen_rounds == [3]


def test_constructor_has_no_baseline_disable_parameter() -> None:
    """§6: «Флага unsafe-отключения в v1 нет» — фиксируем на сигнатуре."""
    params = set(inspect.signature(DocVerifier.__init__).parameters)

    assert params == {
        "self",
        "doc_paths",
        "allowed",
        "repo_root",
        "patch_reader",
        "extra",
    }
