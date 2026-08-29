"""Тесты `DocVerifier` — реализация порта `Verifier` для doc-контуров
(TASK-008, §6 SPEC-002).

Baseline — пять doc-гейтов, гоняются на каждом `verify()` без возможности
отключения: у конструктора нет параметра, которым это можно было бы сделать
(§6, «Флага unsafe-отключения в v1 нет»). `tmp_git_repo` — фикстура
`tests/verifier/conftest.py`.
"""

import inspect
import os
import subprocess
from pathlib import Path

import pytest

from disputatio.contracts.ports import Verifier
from disputatio.contracts.verification import GateStatus, OverallStatus
from disputatio.verifier import GateSpec
from disputatio.verifier.doc_verifier import DocVerifier

# Тот же герметичный набор, что и в conftest.py::_git_env — коммит внутри
# теста (сверх начального коммита фикстуры) обязан быть застрахован от
# global/system gitconfig разработчика той же причиной, что и сама
# фикстура (см. её docstring).
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _GIT_LOCATION_VARS:
        env.pop(key, None)
    return {
        **env,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


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


def test_empty_doc_paths_is_rejected(tmp_git_repo: Path) -> None:
    """Critical (фикс-раунд 1): `doc_paths=()` — отключение baseline типом,
    не булем: цикл по пустому кортежу просто не запускает
    doc-paths/doc-links/doc-anchors/doc-line-refs ни разу, оставляя в
    отчёте один `doc-scope`. §6 запрещает существование ЛЮБОГО способа
    выключить хоть один из пяти гейтов — обязательность `doc_paths`
    закрывает и этот путь.
    """
    with pytest.raises(ValueError):
        DocVerifier(
            doc_paths=(),
            allowed=("spec.md",),
            repo_root=tmp_git_repo,
            patch_reader=lambda round_no: "",
        )


def test_pure_rename_outside_allowed_fails_overall(tmp_git_repo: Path) -> None:
    """Critical (фикс-раунд 1): `git mv` без правки содержимого не печатает
    `---`/`+++` вовсе (только `rename from`/`rename to`) — живой сценарий,
    которым ревьюер воспроизвёл полный обход baseline: четыре content-гейта
    уходят в `skip` (документ по старому пути исчез), а `doc-scope` на
    синтетическом парсере молчал. Патч — настоящий вывод `git diff HEAD`,
    не собранная вручную строка.
    """
    (tmp_git_repo / "spec.md").write_text("# Spec\n", encoding="utf-8")
    _git(tmp_git_repo, "add", "spec.md")
    _git(tmp_git_repo, "commit", "--quiet", "-m", "add spec")
    (tmp_git_repo / "other").mkdir()
    _git(tmp_git_repo, "mv", "spec.md", "other/spec.md")
    patch = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=tmp_git_repo,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout
    assert "rename from spec.md" in patch  # sanity: воспроизведён живой кейс

    verifier = DocVerifier(
        doc_paths=(tmp_git_repo / "other" / "spec.md",),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: patch,
    )

    report = verifier.verify(1)

    assert report.overall is OverallStatus.FAIL
    scope_gate = next(g for g in report.gates if g.name == "doc-scope")
    assert scope_gate.status is GateStatus.FAIL


def test_doc_verifier_is_reexported_from_verifier_package() -> None:
    """Important-1 (фикс-раунд 1): `verifier/__init__.py` заявляет, что
    composition root импортирует отсюда, не из подмодулей — `DocVerifier` и
    doc-гейты обязаны быть в этом списке наравне с `VerifierRunner`."""
    import disputatio.verifier as verifier_pkg

    assert verifier_pkg.DocVerifier is DocVerifier
    for name in (
        "gate_doc_paths",
        "gate_doc_links",
        "gate_doc_anchors",
        "gate_doc_line_refs",
        "gate_doc_scope",
    ):
        assert name in verifier_pkg.__all__
        assert hasattr(verifier_pkg, name)


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
