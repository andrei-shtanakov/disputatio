"""Тесты TASK-025: точный состав errors-блоков sub-config в `pyrefly.toml`
([DESIGN-002], [DESIGN-003]).

Существующий `test_pyrefly_config.py` пинит только набор `matches`;
состав самих релаксаций оставался незакреплённым (backlog TASK-017
«errors blocks unpinned»). Негативные сценарии `test_base.py` передают
лишние поля через `model_validate({...})` с dict-payload — статическая
проверка `unexpected-keyword` на них не срабатывает, поэтому релаксация
файлу не нужна и снимается. Тест фиксирует ужесточённый состав:
`test_base.py` — ровно `read-only = false`, `test_validation.py`
сохраняет обе релаксации (защита от случайного «заодно» при будущих
правках конфига).
"""

import tomllib
from pathlib import Path

_PYREFLY_TOML = Path(__file__).parents[2] / "pyrefly.toml"


def _sub_config_errors(matches: str) -> dict[str, object]:
    """Вернуть errors-блок единственного sub-config с данным matches."""
    with _PYREFLY_TOML.open("rb") as fh:
        config = tomllib.load(fh)
    subs = [sub for sub in config["sub-config"] if sub["matches"] == matches]
    assert len(subs) == 1
    return subs[0]["errors"]


def test_base_errors_exactly_read_only() -> None:
    """errors для test_base.py — ровно {"read-only": false}."""
    errors = _sub_config_errors("tests/contracts/test_base.py")
    assert set(errors) == {"read-only"}
    assert errors["read-only"] is False


def test_validation_errors_keep_both_relaxations() -> None:
    """test_validation.py сохраняет обе релаксации."""
    errors = _sub_config_errors("tests/contracts/test_validation.py")
    assert set(errors) == {"read-only", "unexpected-keyword"}
    assert errors["read-only"] is False
    assert errors["unexpected-keyword"] is False
