"""Evidence-модели и атомарный IO tdd_gate."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate


def test_claim_roundtrip(tmp_path: Path) -> None:
    claim = tdd_gate.Claim(
        task_id="TASK-001",
        selector="tests/x.py::test_y",
        expected_behavior="x",
        baseline_sha="a" * 40,
        red_sha="b" * 40,
        created_at="2026-08-08T00:00:00",
        revision=1,
        test_path="tests/x.py",
    )
    path = tmp_path / "c.json"
    tdd_gate.write_json_atomic(path, claim.to_json())
    loaded = tdd_gate.Claim.from_json(json.loads(path.read_text()))
    assert loaded == claim


def test_atomic_write_no_partial(tmp_path: Path) -> None:
    path = tmp_path / "v.json"
    tdd_gate.write_json_atomic(path, {"k": "v"})
    assert json.loads(path.read_text()) == {"k": "v"}
    assert list(tmp_path.iterdir()) == [path]  # tmp-файл не оставлен


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert tdd_gate.load_claim(tmp_path, "TASK-404") is None
