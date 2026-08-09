"""Тесты активного писателя (I1): TASK-002, [DESIGN-002], [REQ-002].

Импорты `disputatio.core.writers` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор (`test_active_writer_map_is_pinned`) превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
"""

import pytest

from disputatio.contracts import SessionPhase

P = SessionPhase

# Полное отображение по DESIGN-002: `PROPOSING` — автор, остальные активные
# состояния — оркестратор, `IDLE`/`DONE`/`FAILED` — писателя нет.
# Ожидание записано строками: значения enum `Writer` пинятся отдельно.
EXPECTED_WRITER_NAMES: dict[SessionPhase, str | None] = {
    P.IDLE: None,
    P.PROPOSING: "author",
    P.VERIFYING: "orchestrator",
    P.REVIEWING: "orchestrator",
    P.DECIDING: "orchestrator",
    P.CONVERGED: "orchestrator",
    P.DEADLOCK: "orchestrator",
    P.BUDGET_HIT: "orchestrator",
    P.ESCALATED: "orchestrator",
    P.EXPORTING: "orchestrator",
    P.DONE: None,
    P.FAILED: None,
}

ORCHESTRATOR_PHASES = (
    P.VERIFYING,
    P.REVIEWING,
    P.DECIDING,
    P.CONVERGED,
    P.DEADLOCK,
    P.BUDGET_HIT,
    P.ESCALATED,
    P.EXPORTING,
)

NO_WRITER_PHASES = (P.IDLE, P.DONE, P.FAILED)


def test_active_writer_map_is_pinned() -> None:
    """Снимок `ACTIVE_WRITER` совпадает с таблицей DESIGN-002."""
    try:
        from disputatio.core.writers import ACTIVE_WRITER
    except ImportError as exc:  # red-фаза: writers.py ещё не создан
        raise AssertionError("src/disputatio/core/writers.py ещё не создан") from exc

    snapshot = {
        phase: (None if writer is None else str(writer))
        for phase, writer in ACTIVE_WRITER.items()
    }
    assert snapshot == EXPECTED_WRITER_NAMES


def test_writer_enum_values_are_pinned() -> None:
    """`Writer` — ровно два значения: `"author"` и `"orchestrator"`."""
    from disputatio.core.writers import Writer

    assert Writer.AUTHOR.value == "author"
    assert Writer.ORCHESTRATOR.value == "orchestrator"
    assert {member.value for member in Writer} == {"author", "orchestrator"}


def test_proposing_writer_is_author() -> None:
    """`PROPOSING` — единственное состояние, где пишет автор."""
    from disputatio.core.writers import Writer, active_writer

    assert active_writer(P.PROPOSING) is Writer.AUTHOR


@pytest.mark.parametrize("phase", ORCHESTRATOR_PHASES)
def test_non_proposing_active_phases_writer_is_orchestrator(
    phase: SessionPhase,
) -> None:
    """Каждое прочее нетерминальное активное состояние пишет оркестратором."""
    from disputatio.core.writers import Writer, active_writer

    assert active_writer(phase) is Writer.ORCHESTRATOR


@pytest.mark.parametrize("phase", NO_WRITER_PHASES)
def test_idle_and_terminal_phases_have_no_writer(phase: SessionPhase) -> None:
    """`IDLE`/`DONE`/`FAILED` — писателя нет."""
    from disputatio.core.writers import active_writer

    assert active_writer(phase) is None


def test_active_writer_total_over_session_phase() -> None:
    """`ACTIVE_WRITER` тотально по всем 12 состояниям enum `SessionPhase`."""
    from disputatio.core.writers import ACTIVE_WRITER

    assert set(ACTIVE_WRITER.keys()) == set(SessionPhase)


def test_at_most_one_writer_per_phase() -> None:
    """В каждом состоянии писатель ровно один либо его нет — не коллекция."""
    from disputatio.core.writers import ACTIVE_WRITER, Writer

    for phase in SessionPhase:
        writer = ACTIVE_WRITER[phase]
        assert writer is None or isinstance(writer, Writer), phase

    authors = [p for p, w in ACTIVE_WRITER.items() if w is Writer.AUTHOR]
    assert authors == [P.PROPOSING]


def test_active_writer_agrees_with_mapping() -> None:
    """`active_writer` — чистая проекция `ACTIVE_WRITER`, без побочных эффектов."""
    from disputatio.core.writers import ACTIVE_WRITER, active_writer

    before = dict(ACTIVE_WRITER)
    for phase in SessionPhase:
        assert active_writer(phase) is ACTIVE_WRITER[phase], phase
    assert dict(ACTIVE_WRITER) == before
