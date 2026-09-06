"""Симметрия A1–A3 в `DocVerifier`: пропущенный baseline-гейт — не «зелено».

Тот же класс, что и у трёх находок группы A: неизвестность превращается в
успех. `_read_document` отдаёт `skip`, когда документ не существует или не
читается (конвенция `runner.run_gate`, фикс-раунд 1), а `compute_overall`
считает `skip` не-провалом ([DESIGN-009]: отключённый гейт ничего не
опровергает). Каждое из двух решений по отдельности верно, а вместе они
давали дыру: автор spec-контура удаляет спеку — `doc-scope` видит
разрешённый путь и молчит, четыре content-гейта уходят в `skip`, и
`overall` выходит `pass`. Раунд, стерший предмет ревью, получал зелёную
детерминированную часть критерия сходимости.

`DocVerifier` — то место, где это чинится, не ломая ни одного из двух
решений: baseline §6 объявлен неотключаемым (у конструктора нет и параметра
для этого), а «неотключаемый гейт не выполнился» и есть провал.
"""

from pathlib import Path

from disputatio.contracts.verification import GateStatus, OverallStatus
from disputatio.verifier.config import GateSpec
from disputatio.verifier.doc_verifier import DocVerifier

_CONTENT_GATES = ("doc-paths", "doc-links", "doc-anchors", "doc-line-refs")


def test_missing_document_does_not_produce_a_green_report(tmp_git_repo: Path) -> None:
    """Спеки нет — четыре гейта `skip`, и это не `overall == pass`."""
    verifier = DocVerifier(
        doc_paths=(tmp_git_repo / "spec.md",),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
    )

    report = verifier.verify(1)

    skipped = [gate.name for gate in report.gates if gate.status is GateStatus.SKIP]
    assert sorted(skipped) == sorted(_CONTENT_GATES)
    assert report.overall is OverallStatus.FAIL


def test_deleting_the_document_under_review_is_not_a_green_round(
    tmp_git_repo: Path,
) -> None:
    """Сценарий целиком: автор удалил предмет ревью, `doc-scope` молчит.

    Патч трогает только разрешённый путь, то есть границу контура раунд не
    нарушил, — и всё же проверено не было ничего.
    """
    spec = tmp_git_repo / "spec.md"
    spec.write_text("# спека\n", encoding="utf-8")
    patch = (
        "diff --git a/spec.md b/spec.md\n"
        "--- a/spec.md\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-# спека\n"
    )
    spec.unlink()
    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: patch,
    )

    report = verifier.verify(1)

    scope = next(gate for gate in report.gates if gate.name == "doc-scope")
    assert scope.status is GateStatus.PASS, "граница контура не нарушена"
    assert report.overall is OverallStatus.FAIL


def test_unreadable_document_does_not_produce_a_green_report(
    tmp_git_repo: Path,
) -> None:
    """Каталог на месте документа — тот же `skip`, тот же вывод."""
    directory_as_doc = tmp_git_repo / "spec.md"
    directory_as_doc.mkdir()
    verifier = DocVerifier(
        doc_paths=(directory_as_doc,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
    )

    report = verifier.verify(1)

    assert report.overall is OverallStatus.FAIL


def test_a_clean_document_still_passes(tmp_git_repo: Path) -> None:
    """Не-вакуумность: читаемый документ без находок остаётся зелёным."""
    spec = tmp_git_repo / "spec.md"
    spec.write_text("# спека\n\nтекст без ссылок\n", encoding="utf-8")
    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
    )

    report = verifier.verify(1)

    assert report.overall is OverallStatus.PASS


def test_skipped_extra_gate_does_not_sink_a_passing_baseline(
    tmp_git_repo: Path,
) -> None:
    """`skip` у `extra`-гейта не роняет отчёт в `indeterminate` (§4.3).

    Правило «нет ни одного выполненного `pass` ⇒ не `pass`» проверяется
    именно здесь: baseline §6 неотключаем, поэтому его выполненные гейты
    и есть то самое свидетельство, а `skip` добавленного конфигом гейта —
    ровно тот случай, ради которого `skip` не считается провалом.
    """
    spec = tmp_git_repo / "spec.md"
    spec.write_text("# спека\n\nтекст без ссылок\n", encoding="utf-8")
    verifier = DocVerifier(
        doc_paths=(spec,),
        allowed=("spec.md",),
        repo_root=tmp_git_repo,
        patch_reader=lambda round_no: "",
        extra=[GateSpec(name="lint", cmd="disputatio-no-such-binary")],
    )

    report = verifier.verify(1)

    assert [gate.status for gate in report.gates if gate.name == "lint"] == [
        GateStatus.SKIP
    ]
    assert report.overall is OverallStatus.PASS
