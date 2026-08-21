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

from .conftest import NS, TASK

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
    project: Path, full_db: Path, table: str, missing_name: str, capsys: object
) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute(f"DELETE FROM {table}")
    conn.close()

    assert run(project) != 0
    assert not artifact(project).exists()
    err = capsys.readouterr().err  # type: ignore[attr-defined]
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


def test_schema_drift_is_named(project: Path, full_db: Path, capsys: object) -> None:
    conn = sqlite3.connect(full_db)
    with conn:
        conn.execute("DROP TABLE tdd_phases")
    conn.close()

    assert run(project) != 0
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "tdd_phases" in err


def test_old_spec_runner_is_refused(
    project: Path, full_db: Path, capsys: object
) -> None:
    assert run(project, version="2.34.0") != 0
    assert not artifact(project).exists()
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "2.34.0" in err


def test_missing_db_is_refused(project: Path, capsys: object) -> None:
    assert run(project) != 0
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "state db" in err.lower()


def test_ambiguous_db_is_refused(project: Path, full_db: Path, capsys: object) -> None:
    from .conftest import seed_full

    seed_full(project / "spec" / ".executor-phase2-state.db")
    assert run(project) != 0
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "state db" in err.lower()
