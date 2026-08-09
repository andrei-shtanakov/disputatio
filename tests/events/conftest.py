"""Фикстура рабочей директории сессии для тестов `disputatio.events`."""

from pathlib import Path

import pytest


@pytest.fixture
def session_root(tmp_path: Path) -> Path:
    """Пустая рабочая директория — `.disputatio/` создаёт `bootstrap_session`."""
    return tmp_path
