"""Supersession устаревших тестов итерации 1 (ADR-006).

Байт-неизменность тест-файлов после red-чекпоинта не позволяет править
`test_ports.py`; тесты ниже фиксировали семантику `tokens_used == 0`,
отменённую оператором ([REQ-016]: неизвестно ≠ ноль). Их сценарии
перепокрыты в `test_ports_contracts.py`. Реестр `_SUPERSEDED` —
ЕДИНСТВЕННЫЙ санкционированный список исключений: skip применяется
только к перечисленным тестам и только внутри `test_ports.py`.
"""

import pytest

_SUPERSEDED: dict[str, str] = {
    "test_agent_turn_defaults": "superseded by REQ-016: tokens_used=None",
    "test_agent_adapter_fake_runs_via_anyio": (
        "superseded by REQ-016: перепокрыт в test_ports_contracts.py"
    ),
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Помечает superseded-тесты `test_ports.py` скипом с причиной ADR-006."""
    for item in items:
        if item.path.name == "test_ports.py" and item.name in _SUPERSEDED:
            item.add_marker(pytest.mark.skip(reason=_SUPERSEDED[item.name]))
