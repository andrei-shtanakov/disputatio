"""Фикстура: временный git-репо со скелетом под тесты гейта."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate


@pytest.fixture(autouse=True)
def _local_ruff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Локальный ruff для всех тестов гейта.

    `red` с disputatio#7 проверяет lint фиксируемого файла, а tmp-репо фикстур
    не uv-проект: `uv run ruff` внутри него не работает. Подмена автоиспользуемая
    и на весь пакет — иначе каждый тест, вызывающий `red`, падал бы «ruff не
    запускается», то есть fail-closed сработал бы на самом приборе.
    """
    import sys

    monkeypatch.setattr(tdd_gate, "RUFF_CMD", (sys.executable, "-m", "ruff"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    def run(*args: str) -> None:
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "master")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    run("git", "config", "commit.gpgsign", "false")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "spec").mkdir()
    (tmp_path / "tests" / "test_seed.py").write_text(
        "def test_seed():\n    assert True\n"
    )
    (tmp_path / "src" / "mod.py").write_text("X = 1\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")
    return tmp_path


def write_tasks(root: Path, name: str, body: str) -> None:
    """Пишет `root/spec/name` с содержимым `body`, создавая `spec/` при нужде."""
    spec = root / "spec"
    spec.mkdir(exist_ok=True)
    (spec / name).write_text(body)
