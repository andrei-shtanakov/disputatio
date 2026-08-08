"""Тесты модели verification.json: TASK-003, [DESIGN-004], [REQ-004].

Импорты `disputatio.contracts.verification` выполняются внутри тестов: на
момент red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы
collection. Red-селектор (`test_overall_outside_pass_fail_rejected` —
закрытие бэклог-пункта об отклонении невалидного `overall`) превращает
ImportError в AssertionError — гейт принимает red только при падении
assertion'ом.
"""

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError


def spec_4_3_example() -> dict[str, Any]:
    """Пример VerificationReport из SPEC-001 §4.3 (конкретизированы плейсхолдеры)."""
    return {
        "schema": "disputatio/v1",
        "round": 3,
        "gates": [
            {
                "name": "pytest",
                "cmd": "uv run pytest -q",
                "status": "pass",
                "exit_code": 0,
                "duration_s": 12.4,
                "tail": "последние N строк вывода",
            },
            {
                "name": "ruff",
                "cmd": "ruff check .",
                "status": "pass",
                "exit_code": 0,
                "duration_s": 1.3,
                "tail": "All checks passed!",
            },
            {
                "name": "typecheck",
                "cmd": "pyright",
                "status": "skip",
                "exit_code": None,
                "reason": "not configured",
            },
        ],
        "overall": "pass",
        "diff_stats": {"files": 4, "insertions": 120, "deletions": 30},
    }


def test_overall_outside_pass_fail_rejected() -> None:
    """`overall` вне {pass, fail} (в т.ч. "skip") отклоняется ValidationError."""
    try:
        from disputatio.contracts.verification import VerificationReport
    except ImportError as exc:  # red-фаза: verification.py ещё не создан
        raise AssertionError(
            "src/disputatio/contracts/verification.py ещё не создан"
        ) from exc

    for invalid in ("skip", "warn", ""):
        payload = copy.deepcopy(spec_4_3_example())
        payload["overall"] = invalid
        with pytest.raises(ValidationError):
            VerificationReport.model_validate(payload)


def test_spec_4_3_example_validates() -> None:
    """Пример §4.3 валидируется дословно из фикстуры-словаря."""
    from disputatio.contracts.verification import VerificationReport

    report = VerificationReport.model_validate(spec_4_3_example())
    assert report.round == 3
    assert len(report.gates) == 3
    assert report.gates[0].name == "pytest"
    assert report.gates[0].status == "pass"
    assert report.gates[0].exit_code == 0
    assert report.gates[0].duration_s == 12.4
    assert report.overall == "pass"
    assert report.diff_stats.files == 4
    assert report.diff_stats.insertions == 120
    assert report.diff_stats.deletions == 30


def test_empty_gates_allowed() -> None:
    """Пустой список gates допустим (режим analyze); omitted → дефолт []."""
    from disputatio.contracts.verification import VerificationReport

    payload = copy.deepcopy(spec_4_3_example())
    payload["gates"] = []
    report = VerificationReport.model_validate(payload)
    assert report.gates == []

    del payload["gates"]
    assert VerificationReport.model_validate(payload).gates == []


def test_round_trip_with_schema_key() -> None:
    """Round-trip через JSON: ключ `"schema"` присутствует, модель равна."""
    from disputatio.contracts.verification import VerificationReport

    original = VerificationReport.model_validate(spec_4_3_example())
    dumped = original.model_dump_json(by_alias=True)
    assert json.loads(dumped)["schema"] == "disputatio/v1"
    assert VerificationReport.model_validate_json(dumped) == original


def test_skip_gate_exit_code_none_and_reason() -> None:
    """Skip-гейт несёт exit_code=None и reason; duration_s/tail — дефолты."""
    from disputatio.contracts.verification import VerificationReport

    report = VerificationReport.model_validate(spec_4_3_example())
    skipped = report.gates[2]
    assert skipped.status == "skip"
    assert skipped.exit_code is None
    assert skipped.reason == "not configured"
    assert skipped.duration_s is None
    assert skipped.tail == ""


def test_invalid_gate_status_rejected() -> None:
    """Недопустимый `status` гейта (вне pass/fail/skip) отклоняется."""
    from disputatio.contracts.verification import VerificationReport

    payload = copy.deepcopy(spec_4_3_example())
    payload["gates"][0]["status"] = "warn"
    with pytest.raises(ValidationError):
        VerificationReport.model_validate(payload)
