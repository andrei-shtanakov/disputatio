"""Тесты экспортёра TDD-evidence (`scripts/tdd_evidence_export.py`).

Инварианты, которые здесь закреплены:

- неполные данные НЕ материализуются в трекаемый артефакт;
- прежний корректный артефакт при отказе остаётся нетронутым;
- повторный экспорт даёт байт-в-байт тот же файл;
- несовместимая схема или версия — отказ с именами недостающего.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_evidence_export as exporter

from .conftest import CANDIDATE_SHA, CFG_HASH, NS, TASK

VERSION_OK = "2.35.0"


def run(project: Path, **kw: object) -> int:
    """Вызов CLI с подставленной версией spec-runner."""
    argv = [
        "--project-root",
        str(project),
        "--task-id",
        str(kw.pop("task_id", TASK)),
        "--spec-runner-version",
        str(kw.pop("version", VERSION_OK)),
    ]
    return exporter.main(argv)


def artifact(project: Path, ns: str = NS, task: str = TASK) -> Path:
    return project / "spec" / "evidence" / ns / f"{task}.json"


def test_full_chain_is_written(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    data = json.loads(artifact(project).read_text())
    assert data["schema"] == "disputatio/tdd-evidence/v1"
    assert data["complete"] is True
    assert data["task_id"] == TASK
    assert data["namespace"] == NS
    assert data["red"]["outcome"] == "expected_fail"
    assert data["claims"][0]["path"] == "tests/test_x.py"
    assert data["source"]["spec_runner_version"] == VERSION_OK


def test_refactoring_phase_is_carried(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    phases = json.loads(artifact(project).read_text())["phases"]
    assert [p["phase"] for p in phases][-1] == "refactoring"
    assert phases[-1]["detail"] == "skipped"


def test_export_is_idempotent(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    first = artifact(project).read_bytes()
    assert run(project) == 0
    assert artifact(project).read_bytes() == first


def test_json_is_canonical(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    text = artifact(project).read_text()
    keys = [k for k in json.loads(text)]
    assert keys == sorted(keys)
    assert text.endswith("\n")


def test_no_temp_files_left(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    leftovers = list((project / "spec" / "evidence" / NS).glob("*.tmp*"))
    assert leftovers == []


@pytest.mark.parametrize(
    ("table", "missing_name"),
    [
        ("red_checkpoints", "red-checkpoint"),
        ("tdd_claims", "claims"),
        ("gate_verdicts", "gate-verdict:tdd.red"),
    ],
)
def test_missing_rows_refuse_and_name_the_gap(
    project: Path,
    full_db: Path,
    table: str,
    missing_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(f"DELETE FROM {table}")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()
    err = capsys.readouterr().err
    assert missing_name in err


def test_missing_review_verdict_refuses(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("DELETE FROM gate_verdicts WHERE gate_id = 'review'")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_unconfirmed_red_refuses(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("UPDATE red_checkpoints SET outcome = 'not_red'")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_abandoned_red_is_not_evidence(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("UPDATE red_checkpoints SET status = 'abandoned'")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_previous_artifact_survives_a_refusal(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    good = artifact(project).read_bytes()

    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("DELETE FROM tdd_claims")
    conn.close()

    assert run(project) != 0
    assert artifact(project).read_bytes() == good


def test_schema_drift_is_named(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("DROP TABLE tdd_phases")
    conn.close()

    assert run(project) != 0
    err = capsys.readouterr().err
    assert "tdd_phases" in err


def test_old_spec_runner_is_refused(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(project, version="2.34.0") != 0
    assert not artifact(project).exists()
    err = capsys.readouterr().err
    assert "2.34.0" in err


def test_missing_db_is_refused(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(project) != 0
    err = capsys.readouterr().err
    assert "state db" in err.lower()


def test_ambiguous_db_is_refused(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from .conftest import seed_full

    seed_full(project / "spec" / ".executor-phase2-state.db")
    assert run(project) != 0
    err = capsys.readouterr().err
    assert "state db" in err.lower()


def test_claims_from_a_previous_lineage_are_not_evidence(
    project: Path, full_db: Path
) -> None:
    """`repair` открывает новую линию; claims старой — не доказательство этой.

    `tdd_claims.checkpoint_sha` — это red-коммит (`claims.py:314`), поэтому
    привязка проверяема: claim от другого чекпоинта не закрывает требование.
    """
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("UPDATE tdd_claims SET checkpoint_sha = ?", ("f" * 40,))
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_retired_claim_alone_is_not_evidence(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("UPDATE tdd_claims SET status = 'superseded'")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_verdict_under_another_policy_is_stale(
    project: Path, full_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Вердикт ключуется на политике: сменилась — прежний не наследуется."""
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(
            "UPDATE gate_verdicts SET config_hash = ? WHERE gate_id = 'tdd.red'",
            ("ffffffffffffffff",),
        )
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()
    assert "tdd.red" in capsys.readouterr().err


def test_verdicts_must_judge_one_tree(project: Path, full_db: Path) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(
            "UPDATE gate_verdicts SET checkpoint_sha = ? WHERE gate_id = 'review'",
            ("e" * 40,),
        )
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_unsatisfied_verdict_refuses(project: Path, full_db: Path) -> None:
    """До post_review доходят только пройденные гейты — иначе БД противоречива."""
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(
            "UPDATE gate_verdicts SET status = 'unsatisfied' WHERE gate_id = 'review'"
        )
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()


def test_latest_verdict_per_gate_wins(project: Path, full_db: Path) -> None:
    """Более ранний отказ не отменяет более позднего согласия по тому же гейту."""
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(
            "INSERT INTO gate_verdicts (task_id, gate_id, checkpoint_sha, "
            "config_hash, status, detail, timestamp) VALUES (?,?,?,?,?,?,?)",
            (
                TASK,
                "tdd.red",
                CANDIDATE_SHA,
                CFG_HASH,
                "satisfied",
                "повтор после ретрая",
                "2026-08-21T10:12:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE gate_verdicts SET status = 'unsatisfied' "
            "WHERE gate_id = 'tdd.red' AND timestamp = '2026-08-21T10:11:00+00:00'"
        )
    conn.close()

    assert run(project) == 0
    data = json.loads(artifact(project).read_text())
    assert data["gates"]["tdd.red"]["status"] == "satisfied"


def test_judged_tree_is_recorded(project: Path, full_db: Path) -> None:
    assert run(project) == 0
    data = json.loads(artifact(project).read_text())
    assert data["judged_commit"] == CANDIDATE_SHA
    assert data["red"]["commit_sha"] != data["judged_commit"]
