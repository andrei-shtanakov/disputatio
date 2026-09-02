"""RED (TASK-005): BEH-05 — метаданные-подобное содержимое добавленной строки."""


def test_changed_lines_preserves_added_metadata_like_content() -> None:
    """Добавленные строки с содержимым, похожим на diff-маркеры, сохраняются (BEH-05).

    Удалён ровно один первый маркер `+`; всё оставшееся содержимое каждой
    строки сохранено. В частности, добавленная строка с содержимым `++ tail`
    (полная строка патча `+++ tail`) не должна быть перепутана с заголовком
    `+++ ` удалённого файла и отброшена.
    """
    from disputatio.core.oscillation import _changed_lines

    patch = (
        "@@ -1,8 +1,8 @@\n"
        "++\n"
        "+++ tail\n"
        "+-\n"
        "+--\n"
        "+--- tail\n"
        "+@@\n"
        "+diff --git tail\n"
    )

    assert _changed_lines(patch) == {
        "+",
        "++ tail",
        "-",
        "--",
        "--- tail",
        "@@",
        "diff --git tail",
    }
