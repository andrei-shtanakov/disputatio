"""CLI-точка входа `main`: подкоманды `red`/`verify`/`audit` (см. task-5-brief.md)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate

from .conftest import write_tasks

SELECTOR = "tests/test_new.py::test_x"

ONE_RUNNING = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
"""


@pytest.fixture(autouse=True)
def _use_local_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tdd_gate, "PYTEST_CMD", (sys.executable, "-m", "pytest", "-q"))


def test_main_red_requires_selector(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit):
        tdd_gate.main(["red"])


def test_main_red_happy_path_prints_ok_summary(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    (repo / "tests" / "test_new.py").write_text(
        "def test_x():\n    assert False, 'not implemented'\n"
    )

    code = tdd_gate.main(["red", "-k", SELECTOR, "-m", "новая фича"])

    assert code == 0
    out = capsys.readouterr().out
    assert "tdd-gate red: OK" in out
    assert tdd_gate.load_claim(repo, "TASK-001") is not None


def test_main_red_node_id_flag_is_equivalent_to_k(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M1: `--node-id` — основное имя флага (`-k` остаётся алиасом)."""
    monkeypatch.chdir(repo)
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    (repo / "tests" / "test_new.py").write_text(
        "def test_x():\n    assert False, 'not implemented'\n"
    )

    code = tdd_gate.main(["red", "--node-id", SELECTOR, "-m", "новая фича"])

    assert code == 0
    out = capsys.readouterr().out
    assert "tdd-gate red: OK" in out
    claim = tdd_gate.load_claim(repo, "TASK-001")
    assert claim is not None
    assert claim.selector == SELECTOR


def test_main_verify_no_claim_prints_fail_summary(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    write_tasks(repo, "tasks.md", ONE_RUNNING)

    code = tdd_gate.main(["verify"])

    assert code == 1
    out = capsys.readouterr().out
    assert "tdd-gate verify: FAIL" in out


def test_main_audit_no_tasks_prints_ok_summary(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)

    code = tdd_gate.main(["audit"])

    assert code == 0
    out = capsys.readouterr().out
    assert "tdd-gate audit: OK" in out


def test_main_unknown_command_is_argparse_error(repo: Path) -> None:
    with pytest.raises(SystemExit):
        tdd_gate.main(["frobnicate"])
