"""Команда `verify` (+ waiver) и `audit` (см. task-5-brief.md)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate

from .conftest import write_tasks

SELECTOR = "tests/test_new.py::test_x"
TEST_PATH = SELECTOR.split("::")[0]
EXPECTED_BEHAVIOR = "src/mod.py должен содержать READY"

ONE_RUNNING = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
"""

ONE_DONE = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | ✅ DONE
"""


@pytest.fixture(autouse=True)
def _use_local_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет `uv run pytest` на pytest текущего окружения (см. test_red.py)."""
    monkeypatch.setattr(tdd_gate, "PYTEST_CMD", (sys.executable, "-m", "pytest", "-q"))


def _verdict_path(repo: Path, task_id: str = "TASK-001", ns: str = "default") -> Path:
    return repo / "spec" / ".tdd-evidence" / "verdicts" / ns / f"{task_id}.json"


def _history_path(repo: Path, task_id: str = "TASK-001", ns: str = "default") -> Path:
    return (
        repo / "spec" / ".tdd-evidence" / "verdicts" / ns / f"{task_id}.history.jsonl"
    )


def _claim_path(repo: Path, task_id: str = "TASK-001", ns: str = "default") -> Path:
    return repo / "spec" / ".tdd-evidence" / "claims" / ns / f"{task_id}.json"


def _waiver_path(repo: Path, task_id: str = "TASK-001", ns: str = "default") -> Path:
    return repo / "spec" / ".tdd-evidence" / "waivers" / ns / f"{task_id}.json"


def _write_gate_test(repo: Path) -> None:
    """Тест, красный на baseline: `src/mod.py` ещё не содержит `READY`."""
    (repo / "tests" / "test_new.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_x():\n"
        "    assert Path('src/mod.py').read_text() == 'READY\\n'\n"
    )


def _write_trivially_true_test(repo: Path) -> None:
    """Тест, который зелёный независимо от состояния `src/` (для UNEXPECTED_FAIL)."""
    (repo / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n")


def _implement(repo: Path) -> None:
    """Реализация в `src/`, закрывающая `_write_gate_test`; отдельный коммит."""
    (repo / "src" / "mod.py").write_text("READY\n")
    tdd_gate.git(repo, "add", "src/mod.py")
    tdd_gate.git(repo, "commit", "-q", "-m", "impl: mod ready")


def _red_and_implement(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_gate_test(repo)
    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)
    assert code == 0
    _implement(repo)


def _write_claim(
    repo: Path,
    task_id: str = "TASK-001",
    *,
    baseline_sha: str,
    red_sha: str,
    revision: int = 1,
    test_path: str = TEST_PATH,
) -> None:
    tdd_gate.write_json_atomic(
        _claim_path(repo, task_id),
        tdd_gate.Claim(
            task_id=task_id,
            selector=SELECTOR,
            expected_behavior=EXPECTED_BEHAVIOR,
            baseline_sha=baseline_sha,
            red_sha=red_sha,
            created_at="2026-08-08T00:00:00+00:00",
            revision=revision,
            test_path=test_path,
        ).to_json(),
    )


def _write_verdict(
    repo: Path,
    task_id: str = "TASK-001",
    *,
    red_sha: str,
    verified_head: str,
    verdict: str,
) -> None:
    tdd_gate.write_json_atomic(
        _verdict_path(repo, task_id),
        tdd_gate.Verdict(
            task_id=task_id,
            claim_revision=1,
            red_sha=red_sha,
            verified_head=verified_head,
            red_replay="EXPECTED_FAIL",
            selector_at_head="PASS",
            verdict=verdict,
            checked_at="2026-08-08T00:00:00+00:00",
            notes="",
        ).to_json(),
    )


# --- happy path / idempotence / reverification -----------------------------


def test_verify_happy_path_writes_pass_verdict(repo: Path) -> None:
    _red_and_implement(repo)

    code = tdd_gate.cmd_verify(repo)

    assert code == 0
    verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert verdict is not None
    assert verdict.verdict == tdd_gate.CAT_PASS
    assert verdict.red_replay == tdd_gate.CAT_EXPECTED_FAIL
    assert verdict.verified_head == tdd_gate.head_sha(repo)
    claim = tdd_gate.load_claim(repo, "TASK-001")
    assert claim is not None
    assert verdict.red_sha == claim.red_sha


def test_verify_idempotent_pass_on_repeat(repo: Path) -> None:
    _red_and_implement(repo)
    first_code = tdd_gate.cmd_verify(repo)
    first_checked_at = tdd_gate.load_verdict(repo, "TASK-001").checked_at  # type: ignore[union-attr]

    second_code = tdd_gate.cmd_verify(repo)

    assert first_code == 0
    assert second_code == 0
    verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert verdict is not None
    assert verdict.checked_at == first_checked_at, (
        "повтор не должен переписывать verdict"
    )
    assert not _history_path(repo).exists(), "идемпотентный повтор не пишет history"


def test_verify_head_moved_triggers_reverification_and_history(repo: Path) -> None:
    _red_and_implement(repo)
    first_code = tdd_gate.cmd_verify(repo)
    first_verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert first_verdict is not None

    (repo / "README.md").write_text("doc\n")
    tdd_gate.git(repo, "add", "README.md")
    tdd_gate.git(repo, "commit", "-q", "-m", "docs: doc")
    new_head = tdd_gate.head_sha(repo)

    second_code = tdd_gate.cmd_verify(repo)

    assert first_code == 0
    assert second_code == 0
    verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert verdict is not None
    assert verdict.verified_head == new_head
    assert verdict.verdict == tdd_gate.CAT_PASS

    history_lines = _history_path(repo).read_text().splitlines()
    assert len(history_lines) == 1
    archived = json.loads(history_lines[0])
    assert archived["verified_head"] == first_verdict.verified_head


def test_verify_failed_reverification_does_not_archive_prematurely(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M3: провал реверификации не архивирует старый PASS и не плодит дубли.

    PASS@H1 → HEAD двигается на H2 → реверификация падает `GateError`
    (форсируем сбой `git worktree add`, как в тесте на worktree-коллизию)
    ДО того, как появляется новый verdict, который можно было бы записать.
    History обязана остаться пустой, а живой verdict-файл — нетронутым
    старым PASS. Последующий успешный verify архивирует его ровно один
    раз — без дублей от несостоявшейся попытки.
    """
    _red_and_implement(repo)
    assert tdd_gate.cmd_verify(repo) == 0
    first_verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert first_verdict is not None

    (repo / "README.md").write_text("doc\n")
    tdd_gate.git(repo, "add", "README.md")
    tdd_gate.git(repo, "commit", "-q", "-m", "docs: doc")

    collision_dir = tmp_path / "collision-m3"
    collision_dir.mkdir()
    (collision_dir / "stray.txt").write_text("занято до git worktree add\n")
    monkeypatch.setattr(
        tdd_gate.tempfile, "mkdtemp", lambda prefix="": str(collision_dir)
    )

    failed_code = tdd_gate.cmd_verify(repo)

    assert failed_code == 3
    assert not _history_path(repo).exists(), (
        "провал реверификации не должен архивировать старый PASS преждевременно"
    )
    stale = tdd_gate.load_verdict(repo, "TASK-001")
    assert stale is not None
    assert stale.verdict == tdd_gate.CAT_PASS
    assert stale.verified_head == first_verdict.verified_head

    monkeypatch.undo()
    success_code = tdd_gate.cmd_verify(repo)

    assert success_code == 0
    history_lines = _history_path(repo).read_text().splitlines()
    assert len(history_lines) == 1, (
        "не должно быть дублей после отложенного архивирования"
    )


# --- no-claim / waiver -------------------------------------------------------


def test_verify_no_claim_is_fail(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)

    code = tdd_gate.cmd_verify(repo)

    assert code == 1
    assert tdd_gate.load_verdict(repo, "TASK-001") is None


def test_verify_no_claim_valid_waiver_is_waived(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    tdd_gate.write_json_atomic(
        _waiver_path(repo),
        tdd_gate.Waiver(
            task_id="TASK-001",
            reason="согласовано вручную",
            approved_by="human",
            baseline_sha=baseline,
        ).to_json(),
    )

    code = tdd_gate.cmd_verify(repo)

    assert code == 0
    verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert verdict is not None
    assert verdict.verdict == tdd_gate.CAT_WAIVED
    assert verdict.red_replay == ""
    assert verdict.selector_at_head == ""
    assert "согласовано вручную" in verdict.notes


def test_verify_waiver_with_foreign_task_id_is_fail(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    tdd_gate.write_json_atomic(
        _waiver_path(repo),
        tdd_gate.Waiver(
            task_id="TASK-999",
            reason="чужой waiver",
            approved_by="human",
            baseline_sha=baseline,
        ).to_json(),
    )

    code = tdd_gate.cmd_verify(repo)

    assert code == 1
    assert tdd_gate.load_verdict(repo, "TASK-001") is None


def test_verify_waiver_baseline_not_ancestor_is_fail(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    tdd_gate.write_json_atomic(
        _waiver_path(repo),
        tdd_gate.Waiver(
            task_id="TASK-001",
            reason="baseline из будущего/не связан",
            approved_by="human",
            baseline_sha="f" * 40,
        ).to_json(),
    )

    code = tdd_gate.cmd_verify(repo)

    assert code == 1
    assert tdd_gate.load_verdict(repo, "TASK-001") is None


def test_verify_waived_then_claimed_transitions_to_pass_with_history(
    repo: Path,
) -> None:
    """Fix round 1 (MEDIUM): waived -> claimed — легитимный переход, не forgery.

    WAIVED-verdict имеет `red_sha == ""`; сравнение с `claim.red_sha` при
    более позднем `red` не должно трактоваться как подделка (регресс:
    раньше это давало exit 3). Старый WAIVED обязан уйти в history ДО
    перезаписи verdict'а новым PASS.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    tdd_gate.write_json_atomic(
        _waiver_path(repo),
        tdd_gate.Waiver(
            task_id="TASK-001",
            reason="временный waiver",
            approved_by="human",
            baseline_sha=baseline,
        ).to_json(),
    )
    waived_code = tdd_gate.cmd_verify(repo)
    assert waived_code == 0
    waived_verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert waived_verdict is not None
    assert waived_verdict.verdict == tdd_gate.CAT_WAIVED
    # Эвиденс WAIVED-цикла коммитится (реалистичный флоу — иначе `red`
    # споткнётся о них как о запрещённых правках до red-чекпоинта, шаг 4
    # `cmd_red`; это независимо от бага из finding'а, который тут
    # проверяется — совместимости verdict'а с claim'ом).
    tdd_gate.git(repo, "add", "-A")
    tdd_gate.git(repo, "commit", "-q", "-m", "evidence: waived TASK-001")

    _write_gate_test(repo)
    red_code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)
    assert red_code == 0
    _implement(repo)

    verify_code = tdd_gate.cmd_verify(repo)

    assert verify_code == 0
    verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert verdict is not None
    assert verdict.verdict == tdd_gate.CAT_PASS

    history_lines = _history_path(repo).read_text().splitlines()
    assert len(history_lines) == 1
    archived = json.loads(history_lines[0])
    assert archived["verdict"] == tdd_gate.CAT_WAIVED


# --- I2: неизменяемость теста после red ---------------------------------------


def test_verify_test_file_deleted_after_pass_is_rejected(repo: Path) -> None:
    """I2.4(а) — обязательный тест владельца: тест-файл удалён после PASS.

    Удаление зафиксировано новым коммитом (HEAD сдвинулся) — попадает в
    основную цепочку реверификации; новая проверка
    `git diff --quiet red_sha..HEAD -- test_path` ловит это ДО replay.
    """
    _red_and_implement(repo)
    assert tdd_gate.cmd_verify(repo) == 0

    (repo / "tests" / "test_new.py").unlink()
    tdd_gate.git(repo, "add", "-A")
    tdd_gate.git(repo, "commit", "-q", "-m", "чистка: удалить тест-файл")

    code = tdd_gate.cmd_verify(repo)

    assert code == 3


def test_verify_test_weakened_to_vacuous_after_pass_is_rejected(repo: Path) -> None:
    """I2.4(б) — обязательный тест владельца: тело теста выхолощено после PASS.

    Тот же node ID, но тело заменено на `assert True` — тоже новый коммит,
    тоже ловится проверкой неизменяемости test_path.
    """
    _red_and_implement(repo)
    assert tdd_gate.cmd_verify(repo) == 0

    _write_trivially_true_test(repo)
    tdd_gate.git(repo, "add", "-A")
    tdd_gate.git(repo, "commit", "-q", "-m", "выхолащивание теста после PASS")

    code = tdd_gate.cmd_verify(repo)

    assert code == 3


def test_verify_test_rewritten_between_red_and_first_verify_is_error(
    repo: Path,
) -> None:
    """I2 — «выхолащивание МЕЖДУ red и первым verify»: red честный, тест переписан.

    Первый verify (никакого PASS ещё не было) обязан упасть тем же
    механизмом, что и реверификация — тест изменился в диапазоне
    red_sha..HEAD.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_gate_test(repo)
    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)
    assert code == 0

    _write_trivially_true_test(repo)
    tdd_gate.git(repo, "add", "-A")
    tdd_gate.git(repo, "commit", "-q", "-m", "выхолащивание до первого verify")

    code = tdd_gate.cmd_verify(repo)

    assert code == 3


# --- A5: from_json нормализация -----------------------------------------------


def test_verify_claim_with_deleted_field_on_disk_is_clean_error(repo: Path) -> None:
    """A5: claim с удалённым полем на диске → `verify` exit 3, не traceback."""
    _red_and_implement(repo)
    claim_path = _claim_path(repo)
    data = json.loads(claim_path.read_text())
    del data["test_path"]
    claim_path.write_text(json.dumps(data))

    code = tdd_gate.cmd_verify(repo)

    assert code == 3


# --- forged / incompatible verdict, chain violations -------------------------


def test_verify_forged_claim_foreign_task_red_sha_is_error(repo: Path) -> None:
    """C4: trust boundary — сценарий A финального ревью.

    Claim TASK-002 подделан так, что его `red_sha` указывает на честный
    red-коммит ЧУЖОЙ задачи (TASK-001). Без проверки трейлера
    `TDD-Red-Task` verify реиграл бы чужой честный red и мог бы приписать
    его результат TASK-002. Обязан упасть ДО replay: exit 3.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_gate_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    baseline = tdd_gate.head_sha(repo)
    honest_red_sha = tdd_gate.commit_red(
        repo, "TASK-001", baseline, SELECTOR, "default"
    )

    write_tasks(
        repo,
        "tasks.md",
        "## Milestone\n"
        "### TASK-001: Первая\n- Приоритет: P1 | ✅ DONE\n"
        "### TASK-002: Вторая\n- Приоритет: P1 | 🔄 IN_PROGRESS\n",
    )
    _write_claim(repo, "TASK-002", baseline_sha=baseline, red_sha=honest_red_sha)

    code = tdd_gate.cmd_verify(repo)

    assert code == 3
    assert tdd_gate.load_verdict(repo, "TASK-002") is None


def test_verify_forged_verdict_foreign_red_sha_is_error(repo: Path) -> None:
    _red_and_implement(repo)
    claim = tdd_gate.load_claim(repo, "TASK-001")
    assert claim is not None
    _write_verdict(
        repo,
        red_sha="f" * 40,
        verified_head=tdd_gate.head_sha(repo),
        verdict=tdd_gate.CAT_PASS,
    )

    code = tdd_gate.cmd_verify(repo)

    assert code == 3


def test_verify_bad_baseline_sha_is_error_not_traceback(repo: Path) -> None:
    """Fix round 1 (HIGH): битый baseline_sha → чистый exit 3, не traceback.

    Claim с несуществующим `baseline_sha` при валидном `red_sha` раньше
    падал сырым `CalledProcessError` из `_diff_paths` (`git diff` не может
    резолвить baseline). Симметричная проверка `_commit_exists` для
    `baseline_sha` (рядом с уже существовавшей для `red_sha`) ловит это
    раньше, вместе с `_diff_paths`, обёрнутым в `GateError` на случай
    прочих сбоев `git diff`.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_gate_test(repo)
    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)
    assert code == 0
    claim = tdd_gate.load_claim(repo, "TASK-001")
    assert claim is not None
    _write_claim(repo, baseline_sha="a" * 40, red_sha=claim.red_sha)

    code = tdd_gate.cmd_verify(repo)

    assert code == 3


def test_verify_worktree_add_failure_is_error_and_cleans_up(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fix round 1 (HIGH): сбой `git worktree add` → exit 3, worktree list чист.

    Форсируем коллизию: подменяем `tempfile.mkdtemp` на директорию,
    заранее занятую посторонним файлом — `git worktree add` с непустой
    целевой директорией фейлится `CalledProcessError`. Раньше это
    всплывало сырым traceback'ом вместо контрактного exit 3; cleanup
    (`remove --force` -> `prune` при неудаче) обязан не оставить
    репозиторий с висящей worktree-регистрацией.
    """
    _red_and_implement(repo)
    collision_dir = tmp_path / "collision"
    collision_dir.mkdir()
    (collision_dir / "stray.txt").write_text("занято до git worktree add\n")
    monkeypatch.setattr(
        tdd_gate.tempfile, "mkdtemp", lambda prefix="": str(collision_dir)
    )

    code = tdd_gate.cmd_verify(repo)

    assert code == 3
    worktree_list = tdd_gate.git(repo, "worktree", "list", "--porcelain")
    assert str(collision_dir) not in worktree_list


def test_verify_red_sha_not_ancestor_is_error(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    _write_gate_test(repo)
    code = tdd_gate.cmd_red(repo, SELECTOR, EXPECTED_BEHAVIOR)
    assert code == 0
    tdd_gate.git(repo, "reset", "--hard", baseline)

    code = tdd_gate.cmd_verify(repo)

    assert code == 3


def test_verify_diff_touches_src_is_error(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    _write_gate_test(repo)
    (repo / "src" / "mod.py").write_text("READY\n")
    tdd_gate.git(repo, "add", "-A")
    tdd_gate.git(repo, "commit", "-q", "-m", "нарушение: red трогает src")
    red_sha = tdd_gate.head_sha(repo)
    _write_claim(repo, baseline_sha=baseline, red_sha=red_sha)

    code = tdd_gate.cmd_verify(repo)

    assert code == 3


def test_verify_replay_green_is_unexpected_fail(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    _write_trivially_true_test(repo)
    tdd_gate.git(repo, "add", "tests/test_new.py")
    red_sha = tdd_gate.commit_red(repo, "TASK-001", baseline, SELECTOR, "default")
    _write_claim(repo, baseline_sha=baseline, red_sha=red_sha)

    code = tdd_gate.cmd_verify(repo)

    assert code == 1
    verdict = tdd_gate.load_verdict(repo, "TASK-001")
    assert verdict is not None
    assert verdict.verdict == tdd_gate.CAT_UNEXPECTED_FAIL
    assert verdict.red_replay == tdd_gate.CAT_UNEXPECTED_FAIL


# --- audit -------------------------------------------------------------------


def test_audit_done_with_claim_missing_verdict_is_error(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_DONE)
    _write_claim(repo, baseline_sha="a" * 40, red_sha="b" * 40)

    code = tdd_gate.cmd_audit(repo)

    assert code == 3


def test_audit_done_without_claim_is_ok(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_DONE)

    assert tdd_gate.cmd_audit(repo) == 0


def test_audit_done_with_claim_and_pass_verdict_is_ok(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_DONE)
    _write_claim(repo, baseline_sha="a" * 40, red_sha="b" * 40)
    _write_verdict(
        repo, red_sha="b" * 40, verified_head="c" * 40, verdict=tdd_gate.CAT_PASS
    )

    assert tdd_gate.cmd_audit(repo) == 0


def test_audit_done_with_claim_and_waived_verdict_is_ok(repo: Path) -> None:
    write_tasks(repo, "tasks.md", ONE_DONE)
    _write_claim(repo, baseline_sha="a" * 40, red_sha="b" * 40)
    _write_verdict(
        repo, red_sha="b" * 40, verified_head="c" * 40, verdict=tdd_gate.CAT_WAIVED
    )

    assert tdd_gate.cmd_audit(repo) == 0


def test_audit_no_tasks_files_is_ok(repo: Path) -> None:
    assert tdd_gate.cmd_audit(repo) == 0
