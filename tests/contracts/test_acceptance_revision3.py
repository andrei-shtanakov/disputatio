"""Тест TASK-026: полнота evidence ревизии 3 в `spec/.tdd-evidence` (REQ-004).

Финальная приёмка ревизии 3 требует полного evidence-набора дисциплины
TDD-гейта в namespace `ws-w-contracts`: claims TASK-024…TASK-026 и
verdicts TASK-024…TASK-025 со статусом PASS. Verdict самой TASK-026
пишет `tdd_gate verify` ПОСЛЕ этого теста и потому здесь не проверяется
(паттерн приёмки TASK-021, `test_acceptance_revision2.py`). Тест пинит
состояние evidence как наблюдаемое поведение репозитория: пропажа или
провал любого артефакта валит приёмку.
"""

import json
from pathlib import Path

_EVIDENCE = Path(__file__).parents[2] / "spec" / ".tdd-evidence"
_NAMESPACE = "ws-w-contracts"
_REVISION3_TASKS = ("TASK-024", "TASK-025")


def test_revision3_evidence_complete() -> None:
    """Claims TASK-024…TASK-026 на месте, verdicts TASK-024…TASK-025 — PASS."""
    claims_dir = _EVIDENCE / "claims" / _NAMESPACE
    verdicts_dir = _EVIDENCE / "verdicts" / _NAMESPACE
    missing_claims = [
        task_id
        for task_id in (*_REVISION3_TASKS, "TASK-026")
        if not (claims_dir / f"{task_id}.json").is_file()
    ]
    assert missing_claims == []
    verdicts = {
        task_id: json.loads(
            (verdicts_dir / f"{task_id}.json").read_text(encoding="utf-8")
        )["verdict"]
        for task_id in _REVISION3_TASKS
    }
    assert verdicts == dict.fromkeys(_REVISION3_TASKS, "PASS")
