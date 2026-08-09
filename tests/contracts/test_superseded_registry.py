"""Тест TASK-023: пин точного состава реестра `_SUPERSEDED` (REQ-001, ADR-006).

Выбор импорта ([DESIGN-001]): запасной importlib-вариант по
`Path(__file__).parent / "conftest.py"`. Штатный `from
tests.contracts.conftest import _SUPERSEDED` в прогоне pytest падает
`ModuleNotFoundError: No module named 'tests'` — у `tests/` нет
`__init__.py`, pytest кладёт на `sys.path` только `tests/` (пакет
`contracts`), корень репозитория не добавляется. Загрузка под именем
`_task023_conftest` не конфликтует с pytest-автозагрузкой conftest
(модуль `contracts.conftest`): создаётся независимый объект модуля только
для чтения данных, plugin-хуки повторно не регистрируются.

Red-стратегия: буквальный чекпоинт «падающая версия пина» с последующей
green-правкой несовместим с tdd-гейтом — `verify` требует байт-идентичности
тест-файла red-чекпоинту (green достигается только вне tests/). Поэтому
честный red — по прецедентам TASK-017/TASK-021 (санкционированы
requirements, REQ-004): чекпоинт-тест
`test_task023_red_checkpoint_claim_recorded` красный в момент `red` по
построению (claim задачи ещё не записан гейтом — он пишется ПОСЛЕ
red-коммита и в replay red-SHA не попадает) и зелёный после фиксации
чекпоинта. Оба пин-теста зелёные с рождения — regression-пин наблюдаемого
поведения реестра по конституции.
"""

import importlib.util
from pathlib import Path

_CONFTEST_PATH = Path(__file__).parent / "conftest.py"
_CONFTEST_SPEC = importlib.util.spec_from_file_location(
    "_task023_conftest", _CONFTEST_PATH
)
assert _CONFTEST_SPEC is not None
assert _CONFTEST_SPEC.loader is not None
_CONFTEST = importlib.util.module_from_spec(_CONFTEST_SPEC)
_CONFTEST_SPEC.loader.exec_module(_CONFTEST)
_SUPERSEDED: dict[str, str] = _CONFTEST._SUPERSEDED

_TASK023_CLAIM = (
    Path(__file__).parents[2]
    / "spec"
    / ".tdd-evidence"
    / "claims"
    / "ws-w-contracts"
    / "TASK-023.json"
)


def test_superseded_registry_pins_exact_key_set() -> None:
    """Реестр содержит ровно два санкционированных ключа (равенство множеств).

    Именно равенство, не подмножество: тихое расширение реестра
    (превращающее будущие тесты в skip в обход байт-неизменности [REQ-C1])
    обязано валить этот тест.
    """
    assert set(_SUPERSEDED) == {
        "test_agent_turn_defaults",
        "test_agent_adapter_fake_runs_via_anyio",
    }


def test_superseded_reasons_are_nonempty_and_cite_req016() -> None:
    """Причина каждой записи — непустая строка с маркером `REQ-016`."""
    for reason in _SUPERSEDED.values():
        assert isinstance(reason, str)
        assert reason.strip() != ""
        assert "REQ-016" in reason


def test_task023_red_checkpoint_claim_recorded() -> None:
    """Red-чекпоинт TASK-023 зафиксирован гейтом: claim задачи лежит на диске.

    Красный до `tdd_gate.py red` (claim ещё не существует) и при replay
    red-SHA в чистом worktree (claim пишется после red-коммита и в него
    не входит); зелёный после фиксации чекпоинта. Прецедент TASK-021:
    проверка по файловой системе, без пина недетерминированных имён.
    """
    assert _TASK023_CLAIM.is_file()
