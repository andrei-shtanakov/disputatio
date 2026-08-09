"""Тесты `aggregate.compute_overall` (TASK-007, [DESIGN-009], [REQ-008]).

Импорт `disputatio.verifier.aggregate` выполняется внутри тестов: на
момент red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы
collection всего каталога. Red-селектор превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
`disputatio.contracts` существует с первой волны и импортируется обычным
модульным импортом.
"""

from types import ModuleType

import pytest

from disputatio.contracts.verification import GateResult, GateStatus, OverallStatus


def _import_aggregate() -> ModuleType:
    """Импортирует `disputatio.verifier.aggregate`; отсутствие — `AssertionError`."""
    try:
        from disputatio.verifier import aggregate
    except ImportError as exc:  # red-фаза: aggregate.py ещё не создан
        raise AssertionError(
            "src/disputatio/verifier/aggregate.py ещё не создан"
        ) from exc
    return aggregate


def _gate(status: GateStatus, name: str = "tests") -> GateResult:
    """`GateResult` с заданным статусом; остальные поля роли не играют."""
    return GateResult(name=name, cmd=f"run {name}", status=status)


@pytest.mark.parametrize(
    ("statuses", "position"),
    [
        ((GateStatus.FAIL,), "единственный"),
        ((GateStatus.FAIL, GateStatus.PASS, GateStatus.SKIP), "первый"),
        ((GateStatus.PASS, GateStatus.SKIP, GateStatus.FAIL), "последний"),
        ((GateStatus.PASS, GateStatus.FAIL, GateStatus.PASS), "средний"),
    ],
)
def test_any_fail_gate_makes_overall_fail(
    statuses: tuple[GateStatus, ...], position: str
) -> None:
    """Хотя бы один `fail` — независимо от позиции — даёт `overall == fail`."""
    aggregate = _import_aggregate()

    gates = [_gate(status, name=f"gate-{i}") for i, status in enumerate(statuses)]

    assert aggregate.compute_overall(gates) is OverallStatus.FAIL, (
        f"{position} fail-гейт обязан поднять overall в fail"
    )


@pytest.mark.parametrize(
    "statuses",
    [
        (GateStatus.PASS,),
        (GateStatus.SKIP,),
        (GateStatus.PASS, GateStatus.PASS),
        (GateStatus.SKIP, GateStatus.SKIP),
        (GateStatus.PASS, GateStatus.SKIP, GateStatus.PASS),
    ],
)
def test_gates_without_fail_make_overall_pass(
    statuses: tuple[GateStatus, ...],
) -> None:
    """Список из `pass`/`skip` даёт `overall == pass`: `skip` не проваливает."""
    aggregate = _import_aggregate()

    gates = [_gate(status, name=f"gate-{i}") for i, status in enumerate(statuses)]

    assert aggregate.compute_overall(gates) is OverallStatus.PASS


def test_empty_gate_list_makes_overall_pass() -> None:
    """Пустой список gates — `overall == pass` ([DESIGN-009])."""
    aggregate = _import_aggregate()

    assert aggregate.compute_overall([]) is OverallStatus.PASS


def test_compute_overall_does_not_mutate_input() -> None:
    """Функция чистая: список gates и сами `GateResult` остаются прежними."""
    aggregate = _import_aggregate()

    gates = [_gate(GateStatus.PASS, name="a"), _gate(GateStatus.FAIL, name="b")]
    snapshot = [gate.model_dump() for gate in gates]

    aggregate.compute_overall(gates)

    assert len(gates) == 2
    assert [gate.model_dump() for gate in gates] == snapshot
