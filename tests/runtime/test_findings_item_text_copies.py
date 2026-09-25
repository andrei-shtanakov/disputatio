"""Образцы конфига `doc` несут канонический текст findings-item (§5.3, #123).

Канонический текст роли живёт в `contracts.FINDINGS_ITEM_TEXT`, а
операторские образцы формы `document` — литералами: пример в `--help`
(`cli._PIPELINE_FORMS_EPILOG`), текст отказа формы
(`pipeline_config._DOCUMENT_FORM`) и сквозной пример
`docs/document-pipeline.md`. Литералы читаемы, но теперь обязаны совпадать с
константой: оператор, скопировавший образец, иначе получил бы отказ
конфига с кодом 2. Тест разбирает каждый образец как TOML и сверяет пункт,
названный `findings_item`, с константой.
"""

import re
import textwrap
import tomllib
from pathlib import Path
from typing import Any

import pytest

from disputatio import cli
from disputatio.contracts import FINDINGS_ITEM_TEXT
from disputatio.runtime import pipeline_config

_REPO = Path(__file__).resolve().parents[2]


def _document_form_of_epilog() -> str:
    """Блок «одиночный документ» из `--help` — с отступом, до конца строки."""
    _, _, tail = cli._PIPELINE_FORMS_EPILOG.partition("одиночный документ:\n")
    return textwrap.dedent(tail)


def _document_form_of_refusal() -> str:
    """Образец формы `document`, который печатает отказ загрузки конфига."""
    return textwrap.dedent(pipeline_config._DOCUMENT_FORM)


def _document_form_of_guide() -> str:
    """Первый TOML-блок сквозного примера `docs/document-pipeline.md`."""
    text = (_REPO / "docs" / "document-pipeline.md").read_text(encoding="utf-8")
    match = re.search(r"```toml\n(.*?)```", text, flags=re.DOTALL)
    assert match is not None, "в docs/document-pipeline.md нет TOML-блока"
    return match.group(1)


@pytest.mark.parametrize(
    "sample",
    [_document_form_of_epilog, _document_form_of_refusal, _document_form_of_guide],
    ids=["cli-epilog", "config-refusal", "operator-guide"],
)
def test_document_sample_carries_the_canonical_findings_text(sample: Any) -> None:
    """Пункт с ролью в образце — ровно `FINDINGS_ITEM_TEXT`."""
    table = tomllib.loads(sample())["pipeline"]["checklists"]["doc"]

    assert table["items"][table["findings_item"]] == FINDINGS_ITEM_TEXT
