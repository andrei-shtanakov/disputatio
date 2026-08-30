"""A2: пустая evidence-ссылка не является доказательством (SPEC-002 §5.2, V2).

V2 требует непустой `evidence` у каждого пункта чеклиста, «потому что pass
и not_applicable без указания, что именно проверено, — голословны». Схема
проверяла это длиной контейнера: список из одного элемента `{"kind":
"gate", "ref": ""}` удовлетворял `min_length=1`, а содержательность `ref`
не смотрел никто — ни схема, ни `validate_doc_review`. То есть ревью,
объявляющее `approve` по всем пунктам и не называющее ни одного
проверенного объекта, проходило правила, написанные ровно против него.

Тесты закрывают обе половины: пустая/пробельная/невидимая ссылка и
`lines`, не являющийся настоящим положительным диапазоном строк.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from disputatio.contracts.checklist import ArtifactEvidence, ChecklistItem, GateEvidence
from disputatio.contracts.review import Review

# Cf-символы — только escape-последовательностями, без литеральных
# невидимых символов в исходнике (конвенция test_zero_width_hardening).
BLANK_REFS = ("", " ", "   \t\n", "\u200b", "\ufeff", " \u200b ")


def make_review(checklist: list[dict[str, Any]]) -> Review:
    """Doc-ревью `disputatio/v2` с заданным чеклистом."""
    return Review.model_validate(
        {
            "schema": "disputatio/v2",
            "round": 1,
            "role": "reviewer",
            "verdict": "approve",
            "confidence": 0.9,
            "issues": [],
            "checked": ["прочитал спеку"],
            "summary": "всё хорошо",
            "checklist": checklist,
        }
    )


@pytest.mark.parametrize("ref", BLANK_REFS)
def test_gate_evidence_with_blank_ref_rejected(ref: str) -> None:
    """`{"kind": "gate", "ref": ""}` — ссылка ни на какой гейт."""
    with pytest.raises(ValidationError):
        GateEvidence.model_validate({"kind": "gate", "ref": ref})


@pytest.mark.parametrize("ref", BLANK_REFS)
def test_artifact_evidence_with_blank_ref_rejected(ref: str) -> None:
    """Artifact-evidence с пустой ссылкой — диапазон строк в никуда."""
    with pytest.raises(ValidationError):
        ArtifactEvidence.model_validate(
            {"kind": "artifact", "ref": ref, "lines": "34-41"}
        )


@pytest.mark.parametrize("lines", ("0", "0-3", "00", "0-0"))
def test_artifact_evidence_with_nonpositive_lines_rejected(lines: str) -> None:
    """Строки нумеруются с единицы: `lines: "0"` не указывает ни на что."""
    with pytest.raises(ValidationError):
        ArtifactEvidence.model_validate(
            {"kind": "artifact", "ref": "spec/design.md", "lines": lines}
        )


def test_checklist_item_with_blank_gate_ref_rejected() -> None:
    """Пункт с непустым списком из пустой ссылки — не evidence, а её видимость."""
    with pytest.raises(ValidationError):
        ChecklistItem.model_validate(
            {
                "id": "S1",
                "status": "pass",
                "evidence": [{"kind": "gate", "ref": ""}],
                "issue_ids": [],
            }
        )


def test_blanket_approve_on_blank_evidence_never_becomes_a_review() -> None:
    """Сценарий находки целиком: голословный approve по всем пунктам.

    Полный чеклист контура, каждый пункт `pass`, `issues` пуст, вердикт
    `approve` — и в каждом пункте единственная evidence с пустым `ref`.
    Раньше такой payload собирался в `Review` и доходил до runtime как
    успешное ревью; теперь он отвергается схемой, то есть уходит в
    schema-retry вместе с текстом ошибки.
    """
    blanket = [
        {
            "id": item_id,
            "status": "pass",
            "evidence": [{"kind": "gate", "ref": "  "}],
            "issue_ids": [],
        }
        for item_id in ("S1", "S2", "S3", "S4", "S5")
    ]

    with pytest.raises(ValidationError):
        make_review(blanket)


def test_substantive_evidence_still_accepted() -> None:
    """Не-вакуумность: содержательные формы обеих ветвей union остаются валидными."""
    review = make_review(
        [
            {
                "id": "S1",
                "status": "pass",
                "evidence": [
                    {"kind": "gate", "ref": "doc-links"},
                    {
                        "kind": "artifact",
                        "ref": "spec/design.md",
                        "lines": "34-41",
                    },
                ],
                "issue_ids": [],
            }
        ]
    )

    assert review.checklist is not None
    assert len(review.checklist[0].evidence) == 2


@pytest.mark.parametrize("lines", ("1", "1-1", "34-41", "007"))
def test_positive_line_ranges_still_accepted(lines: str) -> None:
    """Не-вакуумность второй половины: настоящий диапазон не должен краснеть."""
    evidence = ArtifactEvidence.model_validate(
        {"kind": "artifact", "ref": "spec/design.md", "lines": lines}
    )

    assert evidence.lines == lines
