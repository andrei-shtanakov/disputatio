"""Агрегация итогового статуса verification ([DESIGN-009], [REQ-008]).

Живёт отдельно от `VerifierRunner`, чтобы правило «один `fail` валит
раунд» проверялось на списке `GateResult` без запуска процессов.
"""

from disputatio.contracts.verification import GateResult, GateStatus, OverallStatus


def compute_overall(gates: list[GateResult]) -> OverallStatus:
    """Агрегация §4.3, три правила сверху вниз, первое сработавшее.

    `fail` при хотя бы одном `fail`-гейте; иначе `pass` при хотя бы одном
    выполненном `pass`; иначе `indeterminate` — набор пуст либо состоит из
    одних `skip`.

    `skip` не провал (отключённый или не запустившийся гейт ничего не
    опровергает), но и не свидетельство: раунд, где не выполнено ни одной
    проверки, зелёного вердикта не получает. Прежняя редакция отдавала
    здесь `pass` — fail-open в самом приборе, поскольку `pass` открывает
    дорогу к `CONVERGED`. Единственный законный путь к сходимости без
    выполненных гейтов остался один — карман §5.1 п.2 для `analyze` с
    пустым набором, и решает его `core.deciding`, а не агрегатор.

    Сравнение через `==`, а не `is` — конвенция [REQ-015]/[DESIGN-013]:
    `model_copy(update=...)` и `model_construct` кладут в поле сырую
    строку без ревалидации, и `is`-сравнение молча пропустило бы такой
    `fail`. `GateStatus` — `StrEnum`, поэтому `==` ловит оба представления.
    """
    if any(gate.status == GateStatus.FAIL for gate in gates):
        return OverallStatus.FAIL
    if any(gate.status == GateStatus.PASS for gate in gates):
        return OverallStatus.PASS
    return OverallStatus.INDETERMINATE
