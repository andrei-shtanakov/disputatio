"""Резолвер текущей задачи: ровно один IN_PROGRESS/REVIEW по всем spec/*tasks.md."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate


def write_tasks(root: Path, name: str, body: str) -> None:
    spec = root / "spec"
    spec.mkdir(exist_ok=True)
    (spec / name).write_text(body)


ONE_RUNNING = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
"""


def test_single_in_progress(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_maestro_prefix_file(tmp_path: Path) -> None:
    write_tasks(tmp_path, "maestro-tasks.md", ONE_RUNNING)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_review_status_counts(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "🔍 REVIEW"))
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_plain_format_without_emoji(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "IN_PROGRESS"))
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_zero_running_is_error(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "⬜ TODO"))
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_two_running_is_error(tmp_path: Path) -> None:
    body = ONE_RUNNING.replace("⬜ TODO", "🔄 IN_PROGRESS")
    write_tasks(tmp_path, "tasks.md", body)
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_two_files_one_running_each_is_error(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING)
    write_tasks(tmp_path, "maestro-tasks.md", ONE_RUNNING.replace("TASK-00", "KAP-00"))
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)
