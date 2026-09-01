"""Runner под вид пайплайна: SPEC-002 v0.2 §2, §7.1, §7.2; задача 5 плана.

Два утверждения здесь принципиально разной природы, и их нельзя менять
местами. Первое — про ПОВЕДЕНИЕ: документный пайплайн заводит контур `doc`,
и его сходимость терминальна. Второе — про СБОРКУ: политики границы раунда
у него не существует как объекта. Тест «drive() ведёт себя как без политики»
прошёл бы и у политики, всегда отвечающей `proceed`, то есть не отличил бы
P10 от запрещённого «не срабатывает» (§10 SPEC-002).
"""

from pathlib import Path
from typing import Any

import pytest

from disputatio.contracts import (
    BoundaryVerdict,
    PipelineKind,
    PipelinePhase,
    Review,
    TransitionReason,
)

from ._pipeline_stand import (
    ARCHITECTURAL,
    DOCUMENT_PATH,
    SLUG,
    TASK_TEXT,
    Script,
    Stand,
    build_stand,
    live_pair,
)


class _ParksEverything:
    """`RoundBoundaryPolicy`, паркующая по собственному условию.

    Нужна ровно затем, чтобы отличить «обнаружение спрашивает ТУ ЖЕ
    политику» от «обнаружение создаёт свою»: у настоящей
    `ArchitecturalDefectPolicy` оба ответа совпали бы, и расхождение
    осталось бы невидимым.
    """

    def after_deciding(self, review: Review) -> BoundaryVerdict:
        del review
        return BoundaryVerdict.PARK


def _document_scripts(outcome: str = "converged") -> dict[str, Script]:
    return {"doc-r1": Script(outcome=outcome)}


def _document_stand(tmp_path: Path, **kwargs: Any) -> Stand:
    return build_stand(
        tmp_path, _document_scripts(**kwargs), kind=PipelineKind.DOCUMENT
    )


def _pair_stand(tmp_path: Path, scripts: dict[str, Script] | None = None) -> Stand:
    return build_stand(
        tmp_path,
        scripts if scripts is not None else {"spec-r1": Script(), "pair-r1": Script()},
    )


def _edges(state: Any) -> list[tuple[PipelinePhase, PipelinePhase, TransitionReason]]:
    return [(t.from_, t.to, t.reason) for t in state.transitions]


# --- старт и терминал вида document (§2, §7.2) ------------------------


def test_run_seeds_doc_loop_and_first_doc_revision(tmp_path: Path) -> None:
    stand = _document_stand(tmp_path)
    state = stand.runner.run(SLUG, TASK_TEXT)

    assert state.transitions[0].to is PipelinePhase.DOC_LOOP
    assert state.doc_sessions[0].session_id == "doc-r1"
    assert state.spec_sessions == [] and state.pair_sessions == []


def test_converged_doc_session_goes_straight_to_exporting(tmp_path: Path) -> None:
    """Сходимость единственного контура терминальна — второго контура нет."""
    stand = _document_stand(tmp_path)
    state = stand.runner.run(SLUG, TASK_TEXT)

    assert (
        PipelinePhase.DOC_LOOP,
        PipelinePhase.EXPORTING,
        TransitionReason.DOCUMENT_CONVERGED,
    ) in _edges(state)
    assert not any(t.to is PipelinePhase.PAIR_LOOP for t in state.transitions)
    assert state.phase is PipelinePhase.DONE


def test_document_entry_hashes_cover_only_its_document(tmp_path: Path) -> None:
    stand = _document_stand(tmp_path)
    state = stand.runner.run(SLUG, TASK_TEXT)

    assert set(state.doc_sessions[0].entry_hashes) == {DOCUMENT_PATH}


def test_document_escalation_exports_partial(tmp_path: Path) -> None:
    """`DEADLOCK` → `ESCALATED` → частичный экспорт, как и у пары (P7)."""
    stand = _document_stand(tmp_path, outcome="deadlock")
    state = stand.runner.run(SLUG, TASK_TEXT)

    assert (
        PipelinePhase.DOC_LOOP,
        PipelinePhase.ESCALATED,
        TransitionReason.SESSION_DEADLOCK,
    ) in _edges(state)
    assert state.phase is PipelinePhase.DONE
    assert stand.exporter.calls == [SLUG]


# --- P10: политика не конструируется (§7.1, §10) ----------------------


def test_document_pipeline_holds_no_boundary_policy(tmp_path: Path) -> None:
    """P10 проверяется отсутствием ОБЪЕКТА, а не поведением."""
    stand = _document_stand(tmp_path)
    assert dict(stand.runner.boundary_policies) == {}


def test_pair_pipeline_holds_exactly_one_boundary_policy(tmp_path: Path) -> None:
    stand = _pair_stand(tmp_path)
    assert set(stand.runner.boundary_policies) == {"pair"}


def test_drive_and_detection_use_the_same_policy(tmp_path: Path) -> None:
    """Один источник истины: обнаружение парковки спрашивает ТУ ЖЕ политику.

    Тест подменяет реализацию через таблицу — на такую, что паркует по
    собственному условию. Если `_parked_round` создаёт свою
    `ArchitecturalDefectPolicy`, парковку он не признает, и расхождение
    проявится как «resume продолжил сессию, которую надлежало вернуть».
    Тест на ключи таблицы этого не ловит: там обе политики совпадают.
    """
    stand = build_stand(
        tmp_path,
        live_pair(),
        boundary_policies={"pair": _ParksEverything()},
    )
    stand.start()

    assert stand.runner.detect_parked(stand.manifest()) is not None


def test_architectural_return_still_happens(tmp_path: Path) -> None:
    """Регрессия P6 на обновлённом стенде: политика доехала до runner'а.

    Без неё suite позеленел бы на дефолте `{}`, а возврат по архитектурному
    дефекту молча перестал бы происходить.
    """
    stand = _pair_stand(
        tmp_path,
        {
            "spec-r1": Script(),
            "pair-r1": Script(outcome="park", issues=(ARCHITECTURAL,)),
            "spec-r2": Script(),
            "pair-r2": Script(),
        },
    )
    state = stand.runner.run(SLUG, TASK_TEXT)

    assert (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.SPEC_LOOP,
        TransitionReason.ARCHITECTURAL_DEFECT,
    ) in _edges(state)


def test_boundary_policies_argument_has_no_default() -> None:
    """Забытый аргумент — `TypeError`, а не молча обеспиличенный pair-runner.

    Дефолт `{}` был бы худшим из решений: suite позеленел бы, а pair-runner
    потерял бы политику — то есть P6 перестал бы исполняться, и заметил бы
    это только живой прогон.
    """
    from disputatio.runtime.pipeline_runner import PipelineRunner

    with pytest.raises(TypeError):
        PipelineRunner()  # type: ignore[call-arg]


# --- регрессия вида pair (§2) -----------------------------------------


def test_pair_pipeline_edges_unchanged(tmp_path: Path) -> None:
    stand = _pair_stand(tmp_path)
    state = stand.runner.run(SLUG, TASK_TEXT)
    edges = _edges(state)

    assert edges[0] == (
        PipelinePhase.IDLE,
        PipelinePhase.SPEC_LOOP,
        TransitionReason.STARTED,
    )
    assert (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.EXPORTING,
        TransitionReason.PAIR_CONVERGED,
    ) in edges


# --- контракт снапшота чеклистов (§5.3) -------------------------------


def _snapshot(tmp_path: Path, order: tuple[str, str]) -> str:
    """Снапшот чеклистов документного пайплайна с заданным порядком пунктов."""
    stand = build_stand(
        tmp_path,
        _document_scripts(),
        kind=PipelineKind.DOCUMENT,
        doc_checklist_order=order,
    )
    stand.runner.run(SLUG, TASK_TEXT)
    return (stand.pipeline_dir() / "checklists.toml").read_text(encoding="utf-8")


def test_doc_snapshot_keeps_declaration_order(tmp_path: Path) -> None:
    """Порядок объявления — часть чеклиста, снапшот обязан его сохранить."""
    snapshot = _snapshot(tmp_path, ("B3", "B1"))
    assert snapshot.index("B3 =") < snapshot.index("B1 =")


def test_reordering_doc_items_changes_hash(tmp_path: Path) -> None:
    """Два порядка одних условий — разные чеклисты, значит разные байты."""
    first = _snapshot(tmp_path / "a", ("B3", "B1"))
    second = _snapshot(tmp_path / "b", ("B1", "B3"))
    assert first != second


def test_snapshot_carries_findings_item_for_every_contour(tmp_path: Path) -> None:
    """Роль — часть критерия; пустота pair записана, а не подразумевается."""
    stand = _pair_stand(tmp_path)
    stand.runner.run(SLUG, TASK_TEXT)

    snapshot = (stand.pipeline_dir() / "checklists.toml").read_text(encoding="utf-8")
    assert 'findings_item = "S1"' in snapshot
    assert "findings_item = false" in snapshot


def test_boundary_policies_cannot_be_extended_after_build(tmp_path: Path) -> None:
    """Таблица политик собрана ПРИ ПОСТРОЕНИИ и после него не дописывается.

    Половина утверждения P10 — про момент: аксессор, сквозь который в
    документный пайплайн можно доложить политику после сборки, отменял бы
    «объекта этой механики здесь не существует». Тип `Mapping` запрещает
    мутацию только на бумаге, поэтому проверяется отказ на исполнении.
    """
    stand = _document_stand(tmp_path)

    with pytest.raises(TypeError):
        stand.runner.boundary_policies["pair"] = _ParksEverything()  # type: ignore[index]

    assert dict(stand.runner.boundary_policies) == {}
