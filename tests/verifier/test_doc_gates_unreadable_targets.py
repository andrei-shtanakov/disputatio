"""Симметрия A1–A3 в doc-гейтах: нечитаемая цель — не проверенная цель.

`gate_doc_anchors` гасил `OSError` от чтения целевого файла и молча шёл
дальше: якорь не проверен, а гейт зелён и следа не оставил. Тот же класс,
что у A1 и A3 — «не смог проверить» превращается в «проверено». Соседний
`gate_doc_line_refs` при том же условии роняет раунд, то есть расхождение
было и внутри одного модуля.

Вторая половина — форма ошибки. Оба гейта ловили только `OSError`, а
`read_text(encoding="utf-8")` на файле не в UTF-8 поднимает
`UnicodeDecodeError` (это `ValueError`), и он улетал наружу мимо
конвенции «сбой чтения становится строкой отчёта, а не исключением».

Цель, которая быть нечитаемой ВПРАВЕ, — каталог: `[docs](docs/)` —
законная Markdown-ссылка, и следа она давать не должна.
"""

import json
from pathlib import Path

from disputatio.contracts.verification import GateStatus
from disputatio.verifier import doc_gates

_NOT_UTF8 = b"\xff\xfe\x00binary\x00"


def _entries(tail: str | None) -> list[dict[str, object]]:
    return [json.loads(line) for line in (tail or "").splitlines() if line]


def test_anchor_into_an_undecodable_file_fails(tmp_path: Path) -> None:
    """Цель существует, но не читается — якорь не проверен, и это находка."""
    (tmp_path / "other.md").write_bytes(_NOT_UTF8)
    doc = tmp_path / "spec.md"
    doc.write_text("См. [раздел](other.md#нужный).\n", encoding="utf-8")

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    assert _entries(result.tail) == [
        {"code": doc_gates.CODE_UNREADABLE, "target": "other.md#нужный", "line": 1}
    ]


def test_anchor_into_a_directory_stays_silent(tmp_path: Path) -> None:
    """Не-вакуумность: каталог — законная цель ссылки, а не сбой чтения."""
    (tmp_path / "docs").mkdir()
    doc = tmp_path / "spec.md"
    doc.write_text("См. [каталог](docs/#раздел).\n", encoding="utf-8")

    result = doc_gates.gate_doc_anchors(doc, tmp_path)

    assert result.status is GateStatus.PASS
    assert _entries(result.tail) == []


def test_line_ref_into_an_undecodable_file_is_a_report_line(tmp_path: Path) -> None:
    """`UnicodeDecodeError` — не `OSError`, и он улетал наружу исключением."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "logo.bin").write_bytes(_NOT_UTF8)
    doc = tmp_path / "spec.md"
    doc.write_text("См. `assets/logo.bin:2` за деталями.\n", encoding="utf-8")

    result = doc_gates.gate_doc_line_refs(doc, tmp_path)

    assert result.status is GateStatus.FAIL
    assert _entries(result.tail) == [
        {"code": doc_gates.CODE_UNREADABLE, "target": "assets/logo.bin", "line": 1}
    ]


def test_undecodable_document_is_a_skip_not_an_exception(tmp_path: Path) -> None:
    """Сам документ не в UTF-8 — `skip` по конвенции модуля, а не traceback.

    `DocVerifier` превращает `skip` baseline-гейта в `overall == fail`
    (симметрия того же класса), так что зелёным раунд от этого не станет.
    """
    doc = tmp_path / "spec.md"
    doc.write_bytes(_NOT_UTF8)

    for gate in (
        doc_gates.gate_doc_paths,
        doc_gates.gate_doc_links,
        doc_gates.gate_doc_anchors,
        doc_gates.gate_doc_line_refs,
    ):
        result = gate(doc, tmp_path)
        assert result.status is GateStatus.SKIP, gate.__name__
        assert result.reason
