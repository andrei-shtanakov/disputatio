"""RED-фаза TASK-015 (BEH-15): документация сообщает новые граничные контракты.

Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-15,
FR-14 (workstreams/WS-disputatio-57/spec/10-requirements.md).
"""

from disputatio.core.oscillation import _changed_lines


def test_changed_lines_docstring_describes_open_hunk_state() -> None:
    """BEH-15: докстрока `_changed_lines` явно называет состояние открытого ханка."""
    doc = _changed_lines.__doc__
    assert doc is not None
    assert "состояни" in doc.lower(), (
        "докстрока должна явно описывать зависимость классификации строк "
        "от СОСТОЯНИЯ открытого ханка (FR-14), а не только упоминать "
        "заголовки ханка"
    )
