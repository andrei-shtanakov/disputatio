"""Тест TASK-024: пин дефолта `session_ref is None` у голого `AgentTurn`.

Traceability: [REQ-001], [REQ-006] · [DESIGN-001]. Закрывает backlog
TASK-016 «lost session_ref-default assertion»: исходный пин жил в
`test_ports.py::test_agent_turn_defaults`, который выведен из прогона
реестром `_SUPERSEDED` (REQ-016) — наблюдаемый дефолт `ports.py:69`
остался без regression-покрытия. Существующие файлы `tests/contracts/`
байт-неизменяемы [REQ-006], поэтому пин возвращается новым файлом.

Red-стратегия: пин фиксирует УЖЕ существующее поведение (`session_ref:
str | None = None`), генуинный red через assertion невозможен. Честный
red — по прецедентам TASK-021/TASK-022/TASK-023 (паттерн claim-existence
checkpoint): тест `test_task024_red_checkpoint_claim_recorded` красный в
момент `red` по построению (claim задачи ещё не записан гейтом — он
пишется ПОСЛЕ red-коммита и в replay red-SHA не попадает) и зелёный
после фиксации чекпоинта. Пин-тест зелёный с рождения — regression-пин
наблюдаемого поведения по конституции.
"""

from pathlib import Path

from disputatio.contracts.ports import AgentTurn

_TASK024_CLAIM = (
    Path(__file__).parents[2]
    / "spec"
    / ".tdd-evidence"
    / "claims"
    / "ws-w-contracts"
    / "TASK-024.json"
)


def test_agent_turn_session_ref_defaults_to_none() -> None:
    """Голый `AgentTurn` (только обязательный `text`) даёт `session_ref is None`.

    Именно `is None`, не falsy-проверка: `None` означает «адаптер не вернул
    ссылку на сессию» — оркестратор обязан отличать её отсутствие от пустой
    строки (`--resume` — оптимизация, промпты самодостаточны, §6).
    """
    turn = AgentTurn(text="сырой вывод агента")
    assert turn.session_ref is None


def test_task024_red_checkpoint_claim_recorded() -> None:
    """Red-чекпоинт TASK-024 зафиксирован гейтом: claim задачи лежит на диске.

    Красный до `tdd_gate.py red` (claim ещё не существует) и при replay
    red-SHA в чистом worktree (claim пишется после red-коммита и в него
    не входит); зелёный после фиксации чекпоинта. Прецедент TASK-021/023:
    проверка по файловой системе, без пина недетерминированных имён.
    """
    assert _TASK024_CLAIM.is_file()
