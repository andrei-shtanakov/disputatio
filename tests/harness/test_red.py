"""Команда `red`: чекпоинт с идемпотентностью и recovery (см. task-4-brief.md)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate

from .conftest import write_tasks

SELECTOR = "tests/test_new.py::test_x"
TEST_PATH = SELECTOR.split("::")[0]
EXPECTED_BEHAVIOR = "новая фича должна вернуть 42"

ONE_RUNNING = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
"""


@pytest.fixture(autouse=True)
def _use_local_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет `uv run pytest` на pytest текущего окружения.

    Разрешённое отступление (см. task-4-report.md): tmp-репо фикстуры не
    uv-проект, поэтому `uv run pytest -q <selector>` внутри него не
    работает — команда запуска вынесена в модульную константу
    `tdd_gate.PYTEST_CMD`, подмена бьёт только в тестах.
    """
    monkeypatch.setattr(tdd_gate, "PYTEST_CMD", (sys.executable, "-m", "pytest", "-q"))


def _write_expected_fail_test(repo: Path) -> None:
    (repo / "tests" / "test_new.py").write_text(
        "def test_x():\n    assert False, 'not implemented'\n"
    )


def _write_green_test(repo: Path) -> None:
    (repo / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n")


def _write_broken_import_test(repo: Path) -> None:
    (repo / "tests" / "test_new.py").write_text(
        "import nonexistent_module_xyz\n\n\ndef test_x():\n    assert False\n"
    )


def _claim_path(repo: Path, task_id: str = "TASK-001") -> Path:
    return repo / "spec" / ".tdd-evidence" / "claims" / f"{task_id}.json"


def test_red_happy_path_creates_commit_and_claim(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    _write_expected_fail_test(repo)

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 0
    claim = tdd_gate.load_claim(repo, "TASK-001")
    assert claim is not None
    assert claim.task_id == "TASK-001"
    assert claim.selector == SELECTOR
    assert claim.expected_behavior == EXPECTED_BEHAVIOR
    assert claim.baseline_sha == baseline
    assert claim.red_sha == tdd_gate.head_sha(repo)
    assert claim.red_sha != baseline
    assert claim.revision == 1


def test_red_is_idempotent_on_repeat(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_expected_fail_test(repo)

    first_code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)
    sha_after_first = tdd_gate.head_sha(repo)
    log_after_first = tdd_gate.git(repo, "log", "--format=%H")

    second_code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert first_code == 0
    assert second_code == 0
    assert tdd_gate.head_sha(repo) == sha_after_first
    assert tdd_gate.git(repo, "log", "--format=%H") == log_after_first


def test_red_forbidden_change_before_red_is_fail(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    _write_expected_fail_test(repo)
    (repo / "src" / "mod.py").write_text("X = 2\n")

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 1
    assert tdd_gate.head_sha(repo) == baseline
    assert tdd_gate.load_claim(repo, "TASK-001") is None


def test_red_green_selector_is_fail(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    _write_green_test(repo)

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 1
    assert tdd_gate.head_sha(repo) == baseline
    assert tdd_gate.load_claim(repo, "TASK-001") is None


def test_red_broken_import_is_error(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    _write_broken_import_test(repo)

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 3
    assert tdd_gate.head_sha(repo) == baseline
    assert tdd_gate.load_claim(repo, "TASK-001") is None


def test_red_no_in_progress_task_is_error(repo: Path) -> None:
    _write_expected_fail_test(repo)

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 3


def test_red_foreign_pending_claim_is_error(repo: Path) -> None:
    write_tasks(
        repo,
        "tasks.md",
        ONE_RUNNING + "### TASK-002: Вторая\n- Приоритет: P1 | ⬜ TODO\n",
    )
    (repo / "tests" / "test_other.py").write_text("def test_o():\n    assert False\n")
    tdd_gate.git(repo, "add", "tests/test_other.py")
    other_baseline = tdd_gate.head_sha(repo)
    other_sha = tdd_gate.commit_red(
        repo, "TASK-002", other_baseline, "tests/test_other.py::test_o"
    )
    tdd_gate.write_json_atomic(
        _claim_path(repo, "TASK-002"),
        tdd_gate.Claim(
            task_id="TASK-002",
            selector="tests/test_other.py::test_o",
            expected_behavior="что-то",
            baseline_sha=other_baseline,
            red_sha=other_sha,
            created_at="2026-08-08T00:00:00+00:00",
            revision=1,
            test_path="tests/test_other.py",
        ).to_json(),
    )
    _write_expected_fail_test(repo)

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 3
    assert tdd_gate.load_claim(repo, "TASK-001") is None


def test_red_claim_without_commit_and_no_trailer_match_is_error(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    tdd_gate.write_json_atomic(
        _claim_path(repo),
        tdd_gate.Claim(
            task_id="TASK-001",
            selector=SELECTOR,
            expected_behavior=EXPECTED_BEHAVIOR,
            baseline_sha=baseline,
            red_sha="0" * 40,
            created_at="2026-08-08T00:00:00+00:00",
            revision=1,
            test_path=TEST_PATH,
        ).to_json(),
    )
    _write_expected_fail_test(repo)

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 3


def test_red_claim_with_stale_sha_recovers_via_trailer(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_expected_fail_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    real_sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR)
    tdd_gate.write_json_atomic(
        _claim_path(repo),
        tdd_gate.Claim(
            task_id="TASK-001",
            selector=SELECTOR,
            expected_behavior=EXPECTED_BEHAVIOR,
            baseline_sha=baseline,
            red_sha="0" * 40,
            created_at="2026-08-08T00:00:00+00:00",
            revision=1,
            test_path=TEST_PATH,
        ).to_json(),
    )

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 0
    assert tdd_gate.head_sha(repo) == real_sha
    claim = tdd_gate.load_claim(repo, "TASK-001")
    assert claim is not None
    assert claim.red_sha == real_sha, "claim должен быть починен, а не оставлен битым"
    assert claim.baseline_sha == baseline
    assert claim.selector == SELECTOR


def test_red_after_pass_verdict_is_supersession_error_without_new_commit(
    repo: Path,
) -> None:
    """Fix round 1 (HIGH): supersession отсекается ДО commit_red.

    Регресс на осиротевшие коммиты: если бы проверка «claim уже закрыт
    PASS» шла только на записи claim'а (шаг 7), каждый повторный вызов
    `red` после PASS создавал бы новый red-коммит и только потом падал —
    несколько попыток оставляли бы несколько мусорных коммитов. Проверяем
    двумя вызовами подряд, что число коммитов не меняется вообще.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_expected_fail_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    red_sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR)
    tdd_gate.write_json_atomic(
        _claim_path(repo),
        tdd_gate.Claim(
            task_id="TASK-001",
            selector=SELECTOR,
            expected_behavior=EXPECTED_BEHAVIOR,
            baseline_sha=baseline,
            red_sha=red_sha,
            created_at="2026-08-08T00:00:00+00:00",
            revision=1,
            test_path=TEST_PATH,
        ).to_json(),
    )
    tdd_gate.write_json_atomic(
        repo / "spec" / ".tdd-evidence" / "verdicts" / "TASK-001.json",
        tdd_gate.Verdict(
            task_id="TASK-001",
            claim_revision=1,
            red_sha=red_sha,
            verified_head=red_sha,
            red_replay="EXPECTED_FAIL",
            selector_at_head="PASS",
            verdict=tdd_gate.CAT_PASS,
            checked_at="2026-08-08T00:00:00+00:00",
            notes="",
        ).to_json(),
    )
    commit_count_before = tdd_gate.git(repo, "rev-list", "--count", "HEAD")

    first_code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)
    second_code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    commit_count_after = tdd_gate.git(repo, "rev-list", "--count", "HEAD")
    assert first_code == 3
    assert second_code == 3
    assert commit_count_after == commit_count_before


def test_red_ignores_untracked_gitignore_and_maestro_spec_files(repo: Path) -> None:
    """C3: untracked `spec/.gitignore` и `spec/maestro-*.md` не блокируют red.

    Реальный worktree: `spec/.gitignore` пишет spec-runner сам (harness-owned,
    остаётся untracked — git_ops.py:ensure_runtime_gitignore), а
    `spec/maestro-requirements.md` генерирует maestro/spec-runner до и во
    время задачи. Ни то, ни другое не должно попадать в forbidden-правки.
    """
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    _write_expected_fail_test(repo)
    (repo / "spec" / ".gitignore").write_text(
        "# spec-runner runtime state — never commit (managed by spec-runner)\n"
        ".executor-*\n.*task-history.log\n.*spec.lock\n"
    )
    (repo / "spec" / "maestro-requirements.md").write_text("# требования\n")

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 0
    assert tdd_gate.load_claim(repo, "TASK-001") is not None


def test_red_recovers_claim_from_commit_trailer_when_claim_missing(
    repo: Path,
) -> None:
    """Step 8: запись claim упала после commit_red — следующий `red` восстанавливает."""
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_expected_fail_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    red_sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR)
    assert tdd_gate.load_claim(repo, "TASK-001") is None

    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)

    assert code == 0
    assert tdd_gate.head_sha(repo) == red_sha
    claim = tdd_gate.load_claim(repo, "TASK-001")
    assert claim is not None
    assert claim.task_id == "TASK-001"
    assert claim.red_sha == red_sha
    assert claim.baseline_sha == baseline
    assert claim.selector == SELECTOR
