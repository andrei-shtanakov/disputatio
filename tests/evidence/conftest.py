"""Фикстуры синтетической `.executor-state.db` для тестов экспортёра.

БД строится теми же DDL, что и spec-runner (`state.py`), но вручную: тест не
должен зависеть от импортируемости чужого пакета, а экспортёр читает БД, а не
объекты.
"""

import sqlite3
from pathlib import Path

import pytest

DDL = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE TABLE executor_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE phase_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);
CREATE TABLE phase_waivers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    waived_outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    provenance TEXT
);
CREATE TABLE red_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    baseline_sha TEXT NOT NULL,
    selector TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE tdd_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    task_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    checkpoint_sha TEXT NOT NULL,
    path TEXT NOT NULL,
    blob_sha TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE tdd_remedies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    task_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    new_checkpoint_id TEXT
);
CREATE TABLE tdd_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    phase TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);
CREATE TABLE gate_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    gate_id TEXT NOT NULL,
    checkpoint_sha TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);
"""

TASK = "TASK-001"
NS = "ws-w-context"
RED_SHA = "a" * 40
BASE_SHA = "b" * 40
CFG_HASH = "0123456789abcdef"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    return conn


def seed_full(path: Path) -> None:
    """Полная цепочка: подтверждённый red, claim, фазы, гейты, ревью."""
    conn = _connect(path)
    with conn:
        conn.execute(
            "INSERT INTO red_checkpoints (task_id, namespace, commit_sha, "
            "baseline_sha, selector, environment_id, execution_mode, "
            "config_hash, outcome, timestamp, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                TASK,
                NS,
                RED_SHA,
                BASE_SHA,
                "tests/test_x.py::test_y",
                "uv.lock:deadbeef",
                "tdd",
                CFG_HASH,
                "expected_fail",
                "2026-08-21T10:00:00+00:00",
                "active",
            ),
        )
        conn.execute(
            "INSERT INTO tdd_claims (namespace, task_id, checkpoint_id, "
            "checkpoint_sha, path, blob_sha, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                NS,
                TASK,
                "ckpt-1",
                RED_SHA,
                "tests/test_x.py",
                "c" * 40,
                "2026-08-21T10:00:01+00:00",
                "active",
            ),
        )
        for phase, ts in [
            ("ready", "2026-08-21T09:59:00+00:00"),
            ("red_authoring", "2026-08-21T09:59:30+00:00"),
            ("red_verifying", "2026-08-21T10:00:00+00:00"),
            ("green_implementing", "2026-08-21T10:05:00+00:00"),
            ("green_verifying", "2026-08-21T10:09:00+00:00"),
            ("refactoring", "2026-08-21T10:10:00+00:00"),
        ]:
            detail = "skipped" if phase == "refactoring" else None
            conn.execute(
                "INSERT INTO tdd_phases (task_id, namespace, phase, detail, "
                "timestamp) VALUES (?,?,?,?,?)",
                (TASK, NS, phase, detail, ts),
            )
        for gate in ("tdd.red", "tdd.claims", "review"):
            conn.execute(
                "INSERT INTO gate_verdicts (task_id, gate_id, checkpoint_sha, "
                "config_hash, status, detail, timestamp) VALUES (?,?,?,?,?,?,?)",
                (
                    TASK,
                    gate,
                    RED_SHA,
                    CFG_HASH,
                    "satisfied",
                    None,
                    "2026-08-21T10:11:00+00:00",
                ),
            )
    conn.close()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Корень «репо» с каталогом spec/ и без БД — её кладёт тест."""
    (tmp_path / "spec").mkdir()
    return tmp_path


@pytest.fixture
def full_db(project: Path) -> Path:
    """Проект с полной цепочкой в `spec/.executor-state.db`."""
    db = project / "spec" / ".executor-state.db"
    seed_full(db)
    return db
