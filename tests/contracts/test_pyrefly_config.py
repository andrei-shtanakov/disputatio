"""Тесты TASK-017: сужение sub-config matches в `pyrefly.toml` ([DESIGN-015]).

Дисциплина ревизии 2: послабления pyrefly (`read-only`,
`unexpected-keyword`) положены ровно двум унаследованным тест-файлам
(`test_base.py`, `test_validation.py`) — broad-паттерн
`tests/contracts/**` распространял бы их на новые файлы и скрывал
ошибки строгой проверки. Тест парсит сам `pyrefly.toml` и фиксирует
точный набор matches как наблюдаемое поведение конфигурации.
"""

import tomllib
from pathlib import Path

_PYREFLY_TOML = Path(__file__).parents[2] / "pyrefly.toml"


def test_sub_config_matches_exactly_two_legacy_test_files() -> None:
    """sub-config matches — ровно test_base.py и test_validation.py."""
    with _PYREFLY_TOML.open("rb") as fh:
        config = tomllib.load(fh)
    matches = sorted(sub["matches"] for sub in config["sub-config"])
    assert matches == [
        "tests/contracts/test_base.py",
        "tests/contracts/test_validation.py",
    ]
