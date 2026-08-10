"""Git-хелперы: baseline, классификация изменений, red-коммит, recovery."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate

SELECTOR = "tests/test_new.py::test_x"


def write_failing_test(repo: Path) -> None:
    (repo / "tests" / "test_new.py").write_text("def test_x():\n    assert False\n")


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


def test_changed_paths_lists_files_inside_wholly_untracked_dir(repo: Path) -> None:
    """Регресс: полностью новая директория не должна схлопываться в одну запись.

    `spec/` создаётся фикстурой пустой (без seed-файла) — git не отслеживает
    пустые директории. Первый файл внутри неё делает всю директорию
    неотслеживаемой; без `--untracked-files=all` `git status` вернул бы
    просто `spec/`, а не `spec/tasks.md`, и `classify_changes` не смогла бы
    сопоставить запись ни с одним правилом.
    """
    (repo / "spec" / "tasks.md").write_text("### TASK-001\n")
    paths = tdd_gate.changed_paths(repo)
    assert "spec/tasks.md" in paths
    assert "spec/" not in paths


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
    claim_path = "spec/.tdd-evidence/claims/default/TASK-001.json"
    allowed, forbidden = tdd_gate.classify_changes([claim_path], "TASK-001")
    assert allowed == [claim_path]
    assert forbidden == []


def test_classify_changes_claims_file_forbidden_for_other_task() -> None:
    claim_path = "spec/.tdd-evidence/claims/default/TASK-001.json"
    allowed, forbidden = tdd_gate.classify_changes([claim_path], "TASK-002")
    assert forbidden == [claim_path]
    assert allowed == []


def test_commit_red_creates_commit_with_tests_file(repo: Path) -> None:
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR, "default")
    files = tdd_gate.git(repo, "show", "--stat", "--format=", sha)
    assert "tests/test_new.py" in files


def test_commit_red_excludes_spec_tasks_md(repo: Path) -> None:
    write_failing_test(repo)
    (repo / "spec" / "tasks.md").write_text("### TASK-001\n")
    tdd_gate.git(repo, "add", "-A")
    baseline = tdd_gate.head_sha(repo)
    sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR, "default")
    files = tdd_gate.git(repo, "show", "--stat", "--format=", sha)
    assert "spec/tasks.md" not in files


def test_commit_red_message_contains_trailers(repo: Path) -> None:
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR, "ws-w-fsm")
    message = tdd_gate.git(repo, "show", "-s", "--format=%B", sha)
    assert "TDD-Red-Task: TASK-001" in message
    assert f"TDD-Baseline: {baseline}" in message
    assert f"TDD-Selector: {SELECTOR}" in message
    assert "TDD-Namespace: ws-w-fsm" in message


def test_find_red_commit_by_trailer_finds_sha(repo: Path) -> None:
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR, "default")
    assert tdd_gate.find_red_commit_by_trailer(repo, "TASK-001", "default") == sha


def test_find_red_commit_by_trailer_returns_none_without_red_commits(
    repo: Path,
) -> None:
    assert tdd_gate.find_red_commit_by_trailer(repo, "TASK-001", "default") is None


def test_find_red_commit_by_trailer_ignores_foreign_namespace(repo: Path) -> None:
    """Round 3: совпадение по `TASK-001` недостаточно — namespace обязан совпасть."""
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR, "ws-w-events")

    assert tdd_gate.find_red_commit_by_trailer(repo, "TASK-001", "ws-w-fsm") is None


def test_find_red_commit_by_trailer_grep_prefilter_rejects_substring_match(
    repo: Path,
) -> None:
    """Round 4 (N3): grep-предфильтр по подстроке не должен давать ложный матч.

    `git log -F --grep="TDD-Red-Task: TASK-1"` матчит по подстроке и
    коммит с `TDD-Red-Task: TASK-10` — точная сверка через
    `_trailer_value` после предфильтра обязана отсеять такого кандидата.
    """
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    tdd_gate.commit_red(repo, "TASK-10", baseline, SELECTOR, "default")

    assert tdd_gate.find_red_commit_by_trailer(repo, "TASK-1", "default") is None


def test_commit_red_leaves_staged_tasks_md_still_staged(repo: Path) -> None:
    """Регресс (Task 3 долг): staged `spec/tasks.md` не расстейджится commit_red'ом.

    `commit_red` коммитит явным pathspec `tests/` — но не должен трогать
    индекс вне этого pathspec: файл, застейдженный до вызова, обязан
    остаться застейдженным (не `git reset`нутым) после.
    """
    write_failing_test(repo)
    (repo / "spec" / "tasks.md").write_text("### TASK-001\n")
    tdd_gate.git(repo, "add", "-A")
    baseline = tdd_gate.head_sha(repo)
    tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR, "default")
    status = tdd_gate._git_stdout(repo, "status", "--porcelain")
    line = next(l for l in status.splitlines() if "spec/tasks.md" in l)
    assert line.startswith("A "), f"tasks.md больше не staged: {line!r}"


def test_find_red_commit_by_trailer_returns_latest_of_several(repo: Path) -> None:
    """Регресс (Task 3 долг): при нескольких red-коммитах возвращается последний.

    Покрывает и чужой red-коммит между ними (TASK-002), и повторный
    red-коммит для той же задачи (TASK-001 дважды).
    """
    write_failing_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    first_sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR, "default")

    (repo / "tests" / "test_other.py").write_text("def test_o():\n    assert False\n")
    tdd_gate.git(repo, "add", "tests/test_other.py")
    tdd_gate.commit_red(
        repo, "TASK-002", first_sha, "tests/test_other.py::test_o", "default"
    )

    (repo / "tests" / "test_new.py").write_text(
        'def test_x():\n    assert False, "v2"\n'
    )
    tdd_gate.git(repo, "add", "tests/test_new.py")
    second_sha = tdd_gate.commit_red(
        repo, "TASK-001", tdd_gate.head_sha(repo), SELECTOR, "default"
    )

    assert (
        tdd_gate.find_red_commit_by_trailer(repo, "TASK-001", "default") == second_sha
    )
