"""Фикстуры тестов Verifier: tmp git-репозиторий и фабрика `GateSpec`.

Импорт `disputatio.verifier` выполняется лениво внутри фабрики: conftest
собирается pytest'ом и на red-фазе задач, когда пакета ещё может не быть, —
import на уровне модуля сломал бы collection всего каталога. Статической
типизации достаточно `TYPE_CHECKING`-ветки.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from disputatio.verifier import GateSpec


def _git(workdir: Path, *args: str) -> None:
    """Запускает `git *args` в `workdir`; ненулевой код возврата — ошибка."""
    subprocess.run(
        ["git", *args],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Git-репозиторий во временном каталоге: init, `.gitignore`, один коммит."""
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "verifier@tests.local")
    _git(tmp_path, "config", "user.name", "verifier-tests")
    (tmp_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "--quiet", "-m", "init")
    return tmp_path


@pytest.fixture
def make_gate_spec() -> "Callable[..., GateSpec]":
    """Фабрика `GateSpec` с дефолтными name/cmd для лаконичных тестов."""

    def factory(
        name: str = "tests",
        cmd: str = "uv run pytest -q",
        *,
        enabled: bool = True,
    ) -> "GateSpec":
        from disputatio.verifier import GateSpec

        return GateSpec(name=name, cmd=cmd, enabled=enabled)

    return factory
