"""RED: TASK-001 — BEH-01, строки изменения только после заголовка ханка."""

from disputatio.core.oscillation import _changed_lines


def test_changed_lines_requires_open_hunk() -> None:
    """Строки `+`/`-` до первого `@@` игнорируются (BEH-01)."""
    patch = (
        "+leaked addition before hunk\n"
        "-leaked deletion before hunk\n"
        "@@ -1,2 +1,2 @@\n"
        "+added in hunk\n"
        "-removed in hunk\n"
    )

    assert _changed_lines(patch) == {"added in hunk", "removed in hunk"}
