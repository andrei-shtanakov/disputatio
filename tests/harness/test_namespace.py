"""Namespace-резолвер и изоляция evidence между workstream-ами (Round 2, A2/A6)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate

from .conftest import write_tasks

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


def _make_claim(task_id: str, selector: str) -> tdd_gate.Claim:
    return tdd_gate.Claim(
        task_id=task_id,
        selector=selector,
        expected_behavior="дано",
        baseline_sha="a" * 40,
        red_sha="b" * 40,
        created_at="2026-08-08T00:00:00+00:00",
        revision=1,
        test_path=selector.split("::")[0],
    )


# --- резолвер ------------------------------------------------------------


def test_resolve_namespace_ws_branch_maps_to_dashed_id(repo: Path) -> None:
    """A6: `ws/w-fsm` → `ws-w-fsm`."""
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    tdd_gate.git(repo, "checkout", "-b", "ws/w-fsm")

    assert tdd_gate.resolve_namespace(repo) == "ws-w-fsm"


def test_resolve_namespace_non_maestro_is_default(repo: Path) -> None:
    """Вне maestro-mode — всегда `default`, ветка не имеет значения."""
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    tdd_gate.git(repo, "checkout", "-b", "some/random-branch")

    assert tdd_gate.resolve_namespace(repo) == "default"


def test_resolve_namespace_non_maestro_detached_head_is_default(repo: Path) -> None:
    """Вне maestro-mode detached HEAD тоже `default` — ручные прогоны/смоук."""
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    tdd_gate.git(repo, "checkout", "--detach", tdd_gate.head_sha(repo))

    assert tdd_gate.resolve_namespace(repo) == "default"


def test_resolve_namespace_maestro_mode_unexpected_branch_is_gate_error(
    repo: Path,
) -> None:
    """A6: maestro-mode + неожиданная форма ветки → `GateError`, без fallback."""
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    # `repo`-фикстура стартует на `master` — не `ws/*`.

    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_namespace(repo)


def test_resolve_namespace_maestro_mode_task_branch_is_gate_error(repo: Path) -> None:
    """A6: maestro-mode + не-`ws/*`-ветка (например, task-ветка) → `GateError`."""
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    tdd_gate.git(repo, "checkout", "-b", "task/TASK-001-something")

    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_namespace(repo)


def test_resolve_namespace_maestro_mode_multi_slash_branch_is_gate_error(
    repo: Path,
) -> None:
    """Copilot PR #3: `ws/a/b` (лишний `/`) → `GateError`, не тихое схлопывание.

    Без этой проверки `branch.replace("/", "-")` схлопнул бы РАЗНЫЕ ветки
    `ws/a/b` и `ws/a-b` в один и тот же namespace `ws-a-b` — два разных
    workstream'а делили бы evidence.
    """
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    tdd_gate.git(repo, "checkout", "-b", "ws/a/b")

    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_namespace(repo)


def test_resolve_namespace_maestro_mode_detached_head_is_gate_error(
    repo: Path,
) -> None:
    """A6: detached HEAD в maestro-mode → `GateError`, не тихий `default`."""
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    tdd_gate.git(repo, "checkout", "--detach", tdd_gate.head_sha(repo))

    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_namespace(repo)


def test_cmd_red_maestro_mode_wrong_branch_is_error(repo: Path) -> None:
    """A6 (интеграционно): та же граница на уровне команды `red` — exit 3."""
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    (repo / "tests" / "test_new.py").write_text(
        "def test_x():\n    assert False, 'not implemented'\n"
    )

    code = tdd_gate.cmd_red(repo, "tests/test_new.py::test_x", "что-то")

    assert code == 3


# --- изоляция evidence между namespace'ами --------------------------------


def test_task_001_in_different_workstreams_does_not_collide(repo: Path) -> None:
    """A6: TASK-001 в ws/w-fsm и ws/w-events — разные пути, не пересекаются."""
    claim_fsm = _make_claim("TASK-001", "tests/a.py::test_a")
    claim_events = _make_claim("TASK-001", "tests/b.py::test_b")
    tdd_gate.write_json_atomic(
        tdd_gate._claim_path(repo, "TASK-001", "ws-w-fsm"), claim_fsm.to_json()
    )
    tdd_gate.write_json_atomic(
        tdd_gate._claim_path(repo, "TASK-001", "ws-w-events"), claim_events.to_json()
    )

    loaded_fsm = tdd_gate.load_claim(repo, "TASK-001", "ws-w-fsm")
    loaded_events = tdd_gate.load_claim(repo, "TASK-001", "ws-w-events")

    assert loaded_fsm == claim_fsm
    assert loaded_events == claim_events
    assert loaded_fsm != loaded_events
    assert tdd_gate._claim_path(repo, "TASK-001", "ws-w-fsm") != tdd_gate._claim_path(
        repo, "TASK-001", "ws-w-events"
    )


def test_audit_scans_only_current_namespace(repo: Path) -> None:
    """A6: audit смотрит только текущий namespace — чужие нарушения не видит."""
    write_tasks(repo, "maestro-tasks.md", ONE_DONE)
    # Чужой namespace: claim без verdict — было бы нарушением, если бы audit
    # его видел.
    tdd_gate.write_json_atomic(
        tdd_gate._claim_path(repo, "TASK-001", "ws-w-events"),
        _make_claim("TASK-001", "tests/a.py::test_a").to_json(),
    )
    tdd_gate.git(repo, "checkout", "-b", "ws/w-fsm")

    # Свой namespace пуст — DONE без claim'а не нарушение (гейт для этой
    # задачи в ЭТОМ workstream не применялся); чужое нарушение не видно.
    assert tdd_gate.cmd_audit(repo) == 0

    # Свой namespace ТЕПЕРЬ получает claim без verdict — уже нарушение, и
    # оно обнаруживается независимо от чужого namespace выше.
    tdd_gate.write_json_atomic(
        tdd_gate._claim_path(repo, "TASK-001", "ws-w-fsm"),
        _make_claim("TASK-001", "tests/b.py::test_b").to_json(),
    )
    assert tdd_gate.cmd_audit(repo) == 3


def test_history_archives_inside_namespace(repo: Path) -> None:
    """A6: history-файл реверификации лежит внутри namespace, не в default."""
    ns = "ws-w-fsm"
    claim = _make_claim("TASK-001", "tests/a.py::test_a")
    old_verdict = tdd_gate.Verdict(
        task_id="TASK-001",
        claim_revision=1,
        red_sha=claim.red_sha,
        verified_head="c" * 40,
        red_replay=tdd_gate.CAT_EXPECTED_FAIL,
        selector_at_head=tdd_gate.CAT_PASS,
        verdict=tdd_gate.CAT_PASS,
        checked_at="2026-08-08T00:00:00+00:00",
        notes="",
    )

    tdd_gate._archive_and_write_verdict(
        repo,
        task_id="TASK-001",
        previous=old_verdict,
        claim=claim,
        verified_head="d" * 40,
        red_replay=tdd_gate.CAT_EXPECTED_FAIL,
        selector_at_head=tdd_gate.CAT_PASS,
        verdict=tdd_gate.CAT_PASS,
        notes="",
        ns=ns,
    )

    history_path = tdd_gate._history_path(repo, "TASK-001", ns)
    assert history_path.exists()
    assert f"/{ns}/" in str(history_path)
    default_history = tdd_gate._history_path(repo, "TASK-001", "default")
    assert not default_history.exists()


def test_waiver_from_foreign_namespace_is_not_accepted(repo: Path) -> None:
    """A6: waiver из чужого namespace не виден и не принимается."""
    tdd_gate.git(repo, "checkout", "-b", "ws/w-fsm")
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    baseline = tdd_gate.head_sha(repo)
    # Waiver лежит в namespace ДРУГОГО workstream'а.
    tdd_gate.write_json_atomic(
        repo / tdd_gate.EVIDENCE / "waivers" / "ws-w-events" / "TASK-001.json",
        tdd_gate.Waiver(
            task_id="TASK-001",
            reason="одобрено для w-events, не для w-fsm",
            approved_by="human",
            baseline_sha=baseline,
        ).to_json(),
    )

    code = tdd_gate.cmd_verify(repo)

    assert code == 1
    assert tdd_gate.load_verdict(repo, "TASK-001", "ws-w-fsm") is None


def test_two_workstreams_same_task_id_merge_without_evidence_conflict(
    repo: Path,
) -> None:
    """A6: два искусственных WS с TASK-001 сливаются в общую ветку без конфликта.

    Реальный `git merge` (не симуляция): namespace-разделённые пути делают
    объединение evidence двух workstream-ов с ОДИНАКОВЫМ ID задачи
    бесконфликтным на уровне git — каждый файл живёт в своём поддереве.
    """
    claim_fsm = _make_claim("TASK-001", "tests/a.py::test_a")
    claim_events = _make_claim("TASK-001", "tests/b.py::test_b")

    tdd_gate.git(repo, "checkout", "-b", "ws/w-fsm")
    tdd_gate.write_json_atomic(
        tdd_gate._claim_path(repo, "TASK-001", "ws-w-fsm"), claim_fsm.to_json()
    )
    tdd_gate.git(repo, "add", "-A")
    tdd_gate.git(repo, "commit", "-q", "-m", "evidence: w-fsm TASK-001")

    tdd_gate.git(repo, "checkout", "master")
    tdd_gate.git(repo, "checkout", "-b", "ws/w-events")
    tdd_gate.write_json_atomic(
        tdd_gate._claim_path(repo, "TASK-001", "ws-w-events"), claim_events.to_json()
    )
    tdd_gate.git(repo, "add", "-A")
    tdd_gate.git(repo, "commit", "-q", "-m", "evidence: w-events TASK-001")

    tdd_gate.git(repo, "checkout", "master")
    tdd_gate.git(repo, "checkout", "-b", "integration")
    tdd_gate.git(repo, "merge", "--no-ff", "ws/w-fsm", "-m", "merge: w-fsm")
    tdd_gate.git(repo, "merge", "--no-ff", "ws/w-events", "-m", "merge: w-events")

    status = tdd_gate.git(repo, "status", "--porcelain")
    assert status == "", f"merge оставил незакоммиченные/конфликтные пути: {status!r}"
    merged_fsm = tdd_gate.load_claim(repo, "TASK-001", "ws-w-fsm")
    merged_events = tdd_gate.load_claim(repo, "TASK-001", "ws-w-events")
    assert merged_fsm == claim_fsm
    assert merged_events == claim_events


# --- Round 3: TDD-Namespace трейлер red-коммита ---------------------------


def test_red_recovery_ignores_foreign_namespace_after_merge(repo: Path) -> None:
    """Round 3(а): recovery не подхватывает чужой red-коммит после слияния веток.

    Сценарий владельца: `w-runtime` ветвится от слитой `pilot/wave-1`, в
    истории которой red-коммиты всех пяти WS волны 1 с потенциально
    совпадающими `TASK-NNN`. Честный red для TASK-001 создаётся в
    `ws/w-fsm`, ветка сливается в общую, `ws/w-runtime` ветвится ОТТУДА —
    в его истории есть чужой red-коммит с трейлером `TDD-Red-Task:
    TASK-001`, но `TDD-Namespace: ws-w-fsm`. `red` в `ws/w-runtime` для
    ТОГО ЖЕ TASK-001 (коллизия идентичности — обычное дело) обязан пройти
    честный цикл (свой селектор, свой коммит), а не «восстановиться» из
    чужого коммита через recovery (шаг 8 `_cmd_red`, `own_claim is None`).
    """
    tdd_gate.git(repo, "checkout", "-b", "ws/w-fsm")
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    (repo / "tests" / "a.py").write_text("def test_a():\n    assert False, 'w-fsm'\n")
    fsm_code = tdd_gate.cmd_red(repo, "tests/a.py::test_a", "w-fsm версия")
    assert fsm_code == 0
    fsm_claim = tdd_gate.load_claim(repo, "TASK-001", "ws-w-fsm")
    assert fsm_claim is not None
    # Реалистичный флоу — claim коммитится (как WAIVED-эвиденс в Round 1),
    # иначе untracked-файл пережил бы checkout и был бы forbidden-правкой
    # в ws/w-runtime (чужой namespace в allowlist не входит).
    tdd_gate.git(repo, "add", "-A")
    tdd_gate.git(repo, "commit", "-q", "-m", "evidence: w-fsm TASK-001 red")

    tdd_gate.git(repo, "checkout", "master")
    tdd_gate.git(repo, "checkout", "-b", "integration")
    tdd_gate.git(repo, "merge", "--no-ff", "ws/w-fsm", "-m", "merge: w-fsm")

    tdd_gate.git(repo, "checkout", "-b", "ws/w-runtime")
    (repo / "tests" / "b.py").write_text(
        "def test_b():\n    assert False, 'w-runtime'\n"
    )

    runtime_code = tdd_gate.cmd_red(repo, "tests/b.py::test_b", "w-runtime версия")

    assert runtime_code == 0
    runtime_claim = tdd_gate.load_claim(repo, "TASK-001", "ws-w-runtime")
    assert runtime_claim is not None
    assert runtime_claim.red_sha != fsm_claim.red_sha, (
        "recovery не должен был подхватить честный red-коммит ws-w-fsm"
    )
    assert runtime_claim.selector == "tests/b.py::test_b", (
        "claim обязан отражать СВОЙ прогон, а не унаследованные из чужого "
        "коммита baseline/selector"
    )


def test_verify_rejects_claim_with_foreign_namespace_red_commit(repo: Path) -> None:
    """Round 3(б): verify отвергает claim, чей red-коммит несёт чужой TDD-Namespace.

    Red-коммит честно создан для namespace `ws-w-events` (селектор/baseline
    совпадают с тем, что claim заявляет) — но claim лежит в namespace
    `ws-w-fsm` и ссылается на этот же `red_sha`. `TDD-Red-Task` совпадает
    (`TASK-001`), `TDD-Selector`/`TDD-Baseline` тоже — единственное
    расхождение изолирует именно namespace-трейлер (C4, Round 3).
    """
    selector = "tests/a.py::test_a"
    tdd_gate.git(repo, "checkout", "-b", "ws/w-events")
    (repo / "tests" / "a.py").write_text("def test_a():\n    assert False\n")
    tdd_gate.git(repo, "add", "tests/a.py")
    baseline = tdd_gate.head_sha(repo)
    foreign_red_sha = tdd_gate.commit_red(
        repo, "TASK-001", baseline, selector, "ws-w-events"
    )

    tdd_gate.git(repo, "checkout", "master")
    tdd_gate.git(repo, "checkout", "-b", "ws/w-fsm")
    write_tasks(repo, "maestro-tasks.md", ONE_RUNNING)
    claim = tdd_gate.Claim(
        task_id="TASK-001",
        selector=selector,
        expected_behavior="дано",
        baseline_sha=baseline,
        red_sha=foreign_red_sha,
        created_at="2026-08-08T00:00:00+00:00",
        revision=1,
        test_path="tests/a.py",
    )
    tdd_gate.write_json_atomic(
        tdd_gate._claim_path(repo, "TASK-001", "ws-w-fsm"), claim.to_json()
    )

    code = tdd_gate.cmd_verify(repo)

    assert code == 3
    assert tdd_gate.load_verdict(repo, "TASK-001", "ws-w-fsm") is None
