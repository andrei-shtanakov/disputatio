"""Git-хелперы: baseline, классификация изменений, red-коммит, recovery."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate

SELECTOR = "tests/test_new.py::test_x"


def write_failing_test(repo: Path) -> None:
    (repo / "tests" / "test_new.py").write_text(
        "def test_x():\n    assert False\n"
    )


def test_git_runs_command(repo: Path) -> None:
    sha = tdd_gate.git(repo, "rev-parse", "HEAD")
    assert len(sha) == 40


def test_head_sha_matches_rev_parse(repo: Path) -> None:
    assert tdd_gate.head_sha(repo) == tdd_gate.git(repo, "rev-parse", "HEAD")


def test_changed_paths_sees_staged(repo: Path) -> None:
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    assert "tests/test_new.py" in tdd_gate.changed_paths(repo)


def test_changed_paths_sees_unstaged(repo: Path) -> None:
    (repo / "src" / "mod.py").write_text("X = 2\n")
    assert "src/mod.py" in tdd_gate.changed_paths(repo)


def test_changed_paths_sees_untracked(repo: Path) -> None:
    (repo / "tests" / "test_untracked.py").write_text(
        "def test_y():\n    assert True\n"
    )
    assert "tests/test_untracked.py" in tdd_gate.changed_paths(repo)


def test_changed_paths_handles_rename(repo: Path) -> None:
    tdd_gate.git(repo, "mv", "tests/test_seed.py", "tests/test_seed_renamed.py")
    paths = tdd_gate.changed_paths(repo)
    assert "tests/test_seed_renamed.py" in paths
    assert "tests/test_seed.py" in paths


def test_classify_changes_tests_file_allowed() -> None:
    allowed, forbidden = tdd_gate.classify_changes(["tests/test_new.py"], "TASK-001")
    assert allowed == ["tests/test_new.py"]
    assert forbidden == []


def test_classify_changes_src_file_forbidden() -> None:
    allowed, forbidden = tdd_gate.classify_changes(["src/mod.py"], "TASK-001")
    assert forbidden == ["src/mod.py"]
    assert allowed == []


def test_classify_changes_tasks_md_allowed() -> None:
    allowed, forbidden = tdd_gate.classify_changes(["spec/tasks.md"], "TASK-001")
    assert allowed == ["spec/tasks.md"]
    assert forbidden == []


def test_classify_changes_task_history_log_allowed() -> None:
    allowed, forbidden = tdd_gate.classify_changes(
        ["spec/.task-history.log"], "TASK-001"
    )
    assert allowed == ["spec/.task-history.log"]
    assert forbidden == []


def test_classify_changes_claims_file_allowed_for_own_task() -> None:
    claim_path = "spec/.tdd-evidence/claims/TASK-001.json"
    allowed, forbidden = tdd_gate.classify_changes([claim_path], "TASK-001")
    assert allowed == [claim_path]
    assert forbidden == []


def test_classify_changes_claims_file_forbidden_for_other_task() -> None:
    claim_path = "spec/.tdd-evidence/claims/TASK-001.json"
    allowed, forbidden = tdd_gate.classify_changes([claim_path], "TASK-002")
    assert forbidden == [claim_path]
    assert allowed == []


def test_commit_red_creates_commit_with_tests_file(repo: Path) -> None:
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR)
    files = tdd_gate.git(repo, "show", "--stat", "--format=", sha)
    assert "tests/test_new.py" in files


def test_commit_red_excludes_spec_tasks_md(repo: Path) -> None:
    write_failing_test(repo)
    (repo / "spec" / "tasks.md").write_text("### TASK-001\n")
    tdd_gate.git(repo, "add", "-A")
    baseline = tdd_gate.head_sha(repo)
    sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR)
    files = tdd_gate.git(repo, "show", "--stat", "--format=", sha)
    assert "spec/tasks.md" not in files


def test_commit_red_message_contains_trailers(repo: Path) -> None:
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR)
    message = tdd_gate.git(repo, "show", "-s", "--format=%B", sha)
    assert "TDD-Red-Task: TASK-001" in message
    assert f"TDD-Baseline: {baseline}" in message
    assert f"TDD-Selector: {SELECTOR}" in message


def test_find_red_commit_by_trailer_finds_sha(repo: Path) -> None:
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR)
    assert tdd_gate.find_red_commit_by_trailer(repo, "TASK-001") == sha


def test_find_red_commit_by_trailer_returns_none_without_red_commits(
    repo: Path,
) -> None:
    assert tdd_gate.find_red_commit_by_trailer(repo, "TASK-001") is None
