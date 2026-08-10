"""Скип селектора не является доказательством (finding критика ревизии 2).

`_run_selector` классифицировал exit 0 как "green", а skip даёт exit 0 —
селектор, попавший под skip-механизм (marker, conftest-реестр), засчитывался
бы как зелёный. Green обязан требовать фактического "N passed" без skip.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate


def _seed_repo(tmp_path: Path) -> Path:
    def run(*args: str) -> None:
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "master")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    run("git", "config", "commit.gpgsign", "false")
    (tmp_path / "tests").mkdir()
    return tmp_path


def test_skipped_selector_is_not_green(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = _seed_repo(tmp_path)
    (repo / "tests" / "test_skip.py").write_text(
        "import pytest\n\n"
        '@pytest.mark.skip(reason="superseded")\n'
        "def test_answer() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        tdd_gate, "PYTEST_CMD", (sys.executable, "-m", "pytest", "-q")
    )
    category, output = tdd_gate._run_selector(
        repo, "tests/test_skip.py::test_answer"
    )
    assert category != "green", output
    assert "skip" in category or category == "error"


def test_passing_selector_still_green(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = _seed_repo(tmp_path)
    (repo / "tests" / "test_ok.py").write_text(
        "def test_answer() -> None:\n    assert True\n", encoding="utf-8"
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        tdd_gate, "PYTEST_CMD", (sys.executable, "-m", "pytest", "-q")
    )
    category, _ = tdd_gate._run_selector(repo, "tests/test_ok.py::test_answer")
    assert category == "green"
