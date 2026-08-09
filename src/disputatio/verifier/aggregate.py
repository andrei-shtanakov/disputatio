"""Агрегация итогового статуса verification ([DESIGN-009], [REQ-008]).

Живёт отдельно от `VerifierRunner`, чтобы правило «один `fail` валит
раунд» проверялось на списке `GateResult` без запуска процессов.
"""

from disputatio.contracts.verification import GateResult, GateStatus, OverallStatus


def compute_overall(gates: list[GateResult]) -> OverallStatus:
    """`fail`, если есть хотя бы один `fail`-gate; иначе `pass`.

    `skip` — не провал: отключённый или пропущенный гейт ничего не
    доказывает, но и ничего не опровергает, поэтому пустой список и список
    из одних `skip` дают `pass` ([DESIGN-009]). Утечь на верхний уровень
    `skip` не может по типу: `OverallStatus` — enum из двух значений.

    Сравнение через `==`, а не `is` — конвенция [REQ-015]/[DESIGN-013]:
    `model_copy(update=...)` и `model_construct` кладут в поле сырую
    строку без ревалидации, и `is`-сравнение молча пропустило бы такой
    `fail`. `GateStatus` — `StrEnum`, поэтому `==` ловит оба представления.
    Ошибка здесь однонаправленно опасна: ложный `pass` открывает дорогу
    к `CONVERGED` ([REQ-008]).
    """
    if any(gate.status == GateStatus.FAIL for gate in gates):
        return OverallStatus.FAIL
    return OverallStatus.PASS
