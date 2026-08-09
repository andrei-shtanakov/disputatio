"""Тест TASK-021: полнота evidence ревизии 2 в `spec/.tdd-evidence` (NFR-003).

Финальная приёмка ревизии 2 требует полного evidence-набора дисциплины
TDD-гейта в namespace `ws-w-contracts`: claims TASK-013…TASK-021 и
verdicts TASK-013…TASK-020 со статусом PASS. Verdict самой TASK-021
пишет `tdd_gate verify` ПОСЛЕ этого теста и потому здесь не проверяется.
Тест пинит состояние evidence как наблюдаемое поведение репозитория:
пропажа или провал любого артефакта валит приёмку.
"""

import json
from pathlib import Path

_EVIDENCE = Path(__file__).parents[2] / "spec" / ".tdd-evidence"
_NAMESPACE = "ws-w-contracts"
_REVISION2_TASKS = tuple(f"TASK-{number:03d}" for number in range(13, 21))


def test_revision2_evidence_complete() -> None:
    """Claims TASK-013…TASK-021 на месте, verdicts TASK-013…TASK-020 — PASS."""
    claims_dir = _EVIDENCE / "claims" / _NAMESPACE
    verdicts_dir = _EVIDENCE / "verdicts" / _NAMESPACE
    missing_claims = [
        task_id
        for task_id in (*_REVISION2_TASKS, "TASK-021")
        if not (claims_dir / f"{task_id}.json").is_file()
    ]
    assert missing_claims == []
    verdicts = {
        task_id: json.loads(
            (verdicts_dir / f"{task_id}.json").read_text(encoding="utf-8")
        )["verdict"]
        for task_id in _REVISION2_TASKS
    }
    assert verdicts == dict.fromkeys(_REVISION2_TASKS, "PASS")
