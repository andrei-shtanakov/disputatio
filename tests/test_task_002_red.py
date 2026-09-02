"""RED-тест TASK-002 (BEH-02): заголовок следующего файла закрывает ханк.

Импорт `disputatio.core.oscillation` выполняется внутри теста по тому же
принципу, что и в `tests/core/test_oscillation.py`: на момент red-чекпоинта
модуль уже существует (TASK-001 влит), поэтому падение здесь — не
`ImportError`, а `AssertionError` на самой проверяемой семантике FR-02(3).
"""


def test_changed_lines_tracks_multiple_files_and_hunks() -> None:
    """Ханки двух файлов объединяются; метаданные между ними — нет (BEH-02)."""
    from disputatio.core.oscillation import _changed_lines

    patch = (
        "@@ -1,2 +1,2 @@\n"
        "+added in file one\n"
        "-removed in file one\n"
        "diff --git a/two.py b/two.py\n"
        "index 1111111..2222222 100644\n"
        "-- rename similarity 100%\n"
        "--- a/two.py\n"
        "+++ b/two.py\n"
        "@@ -3,2 +3,2 @@\n"
        "+added in file two\n"
        "-removed in file two\n"
    )

    assert _changed_lines(patch) == {
        "added in file one",
        "removed in file one",
        "added in file two",
        "removed in file two",
    }
