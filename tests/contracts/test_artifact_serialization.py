"""Тесты TASK-014: serialize_by_alias и обязательный ключ `schema` ([REQ-014]).

Закрывают две дыры [DESIGN-012], уточняющие [REQ-001]: (1) `model_dump()`
и `model_dump_json()` БЕЗ аргумента `by_alias` обязаны содержать ключ
`"schema"`, а не имя атрибута `"schema_"` — иначе артефакт, записанный на
диск без явного `by_alias=True`, не проходит собственную десериализацию
по alias; (2) `model_validate`/`model_validate_json` без ключа `schema`
обязаны падать `ValidationError` — молчаливый дефолт v1 маскирует чужие
или битые payload'ы. Конструктор (ADR-005) остаётся удобным: версия
подставляется по умолчанию. Новый файл — тест-файлы итерации 1
байт-неизменны (NFR-003).
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.base import SCHEMA_V1, ArtifactBase
from disputatio.contracts.review import Review
from disputatio.contracts.verification import VerificationReport


class Sample(ArtifactBase):
    """Минимальный корневой артефакт для тестов базового контракта."""

    value: int


def review_payload() -> dict[str, Any]:
    """Валидный payload Review (минимальный, по образцу §4.4)."""
    return {
        "schema": "disputatio/v1",
        "round": 3,
        "role": "reviewer",
        "verdict": "request_changes",
        "confidence": 0.8,
        "issues": [
            {
                "id": "R3-1",
                "severity": "blocker",
                "file": "src/x.py",
                "claim": "что не так, проверяемая формулировка",
                "evidence": "подтверждено: цитата diff",
            }
        ],
        "checked": ["прочитал diff"],
        "summary": "1-3 предложения",
    }


def verification_payload() -> dict[str, Any]:
    """Валидный payload VerificationReport (минимальный, по образцу §4.3)."""
    return {
        "schema": "disputatio/v1",
        "round": 3,
        "gates": [],
        "overall": "pass",
        "diff_stats": {"files": 1, "insertions": 2, "deletions": 0},
    }


def test_default_dump_contains_schema_key() -> None:
    """model_dump()/model_dump_json() без by_alias: ключ `schema`, не `schema_`."""
    sample = Sample(value=1)

    dumped = sample.model_dump()
    assert dumped.get("schema") == SCHEMA_V1 == "disputatio/v1"
    assert "schema_" not in dumped

    dumped_json = json.loads(sample.model_dump_json())
    assert dumped_json.get("schema") == SCHEMA_V1
    assert "schema_" not in dumped_json


def test_real_artifacts_default_dump_by_alias() -> None:
    """Review и VerificationReport сериализуются по alias без аргументов."""
    artifacts = (
        Review.model_validate(review_payload()),
        VerificationReport.model_validate(verification_payload()),
    )
    for artifact in artifacts:
        dumped = artifact.model_dump()
        assert dumped.get("schema") == SCHEMA_V1
        assert "schema_" not in dumped

        dumped_json = json.loads(artifact.model_dump_json())
        assert dumped_json.get("schema") == SCHEMA_V1
        assert "schema_" not in dumped_json


def test_validate_without_schema_key_rejected() -> None:
    """model_validate без ключа `schema` → ValidationError, без дефолта v1."""
    with pytest.raises(ValidationError):
        Sample.model_validate({"value": 1})

    review = review_payload()
    del review["schema"]
    with pytest.raises(ValidationError):
        Review.model_validate(review)

    verification = verification_payload()
    del verification["schema"]
    with pytest.raises(ValidationError):
        VerificationReport.model_validate(verification)


def test_validate_json_without_schema_key_rejected() -> None:
    """model_validate_json без ключа `schema` → ValidationError."""
    with pytest.raises(ValidationError):
        Sample.model_validate_json('{"value": 1}')


def test_constructor_without_schema_substitutes_v1() -> None:
    """Конструктор без схемы работает и даёт SCHEMA_V1 (ADR-005)."""
    assert Sample(value=42).schema_ == SCHEMA_V1


def test_round_trip_with_default_arguments() -> None:
    """model_validate_json(model_dump_json()) без аргументов стабилен."""
    originals = (
        Sample(value=42),
        Review.model_validate(review_payload()),
        VerificationReport.model_validate(verification_payload()),
    )
    for original in originals:
        restored = type(original).model_validate_json(original.model_dump_json())
        assert restored == original
