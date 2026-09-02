"""Тесты детектора осцилляции: TASK-005, [DESIGN-006], [REQ-011].

Импорты `disputatio.core.oscillation` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор (`test_patch_similarity_identical_patches_is_one`) превращает
ImportError в AssertionError — гейт принимает red только при падении
assertion'ом.
"""

from types import MappingProxyType

from disputatio.contracts.review import Issue, Severity


def make_issue(
    *, issue_id: str = "i1", file: str = "a.py", claim: str = "off by one"
) -> Issue:
    return Issue(id=issue_id, severity=Severity.MAJOR, file=file, claim=claim)


def test_patch_similarity_identical_patches_is_one() -> None:
    """Идентичные патчи → Jaccard-similarity 1.0."""
    try:
        from disputatio.core.oscillation import patch_similarity
    except ImportError as exc:  # red-фаза: oscillation.py ещё не создан
        raise AssertionError(
            "src/disputatio/core/oscillation.py ещё не создан"
        ) from exc

    patch = "+++ b/a.py\n--- a/a.py\n@@ -1,2 +1,2 @@\n+line one\n-line two\n"
    assert patch_similarity(patch, patch) == 1.0


def test_patch_similarity_disjoint_patches_is_zero() -> None:
    """Непересекающиеся множества изменённых строк → similarity 0.0."""
    from disputatio.core.oscillation import patch_similarity

    a = "+++ b/a.py\n--- a/a.py\n@@ -1,2 +1,2 @@\n+alpha\n-beta\n"
    b = "+++ b/a.py\n--- a/a.py\n@@ -1,2 +1,2 @@\n+gamma\n-delta\n"
    assert patch_similarity(a, b) == 0.0


def test_patch_similarity_excludes_diff_headers() -> None:
    """Заголовки `+++`/`---` не участвуют в множестве изменённых строк."""
    from disputatio.core.oscillation import patch_similarity

    a = "+++ b/a.py\n--- a/a.py\n@@ -1,1 +1,1 @@\n+line\n"
    b = "+++ b/other.py\n--- a/other.py\n@@ -1,1 +1,1 @@\n+line\n"
    assert patch_similarity(a, b) == 1.0


def test_patch_similarity_normalizes_trailing_whitespace() -> None:
    """Хвостовые пробелы после маркера +/- нормализуются перед сравнением."""
    from disputatio.core.oscillation import patch_similarity

    a = "@@ -1,2 +1,2 @@\n+line   \n-other\t\n"
    b = "@@ -1,2 +1,2 @@\n+line\n-other\n"
    assert patch_similarity(a, b) == 1.0


def test_patch_similarity_two_empty_patches_is_one() -> None:
    """Два пустых патча подряд → 1.0 (автор дважды не сделал ничего)."""
    from disputatio.core.oscillation import patch_similarity

    assert patch_similarity("", "") == 1.0


def test_changed_lines_requires_open_hunk() -> None:
    """Строки `+`/`-` до первого `@@` игнорируются (BEH-01)."""
    from disputatio.core.oscillation import _changed_lines

    patch = (
        "+leaked addition before hunk\n"
        "-leaked deletion before hunk\n"
        "@@ -1,2 +1,2 @@\n"
        "+added in hunk\n"
        "-removed in hunk\n"
    )
    assert _changed_lines(patch) == {"added in hunk", "removed in hunk"}


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


def test_changed_lines_empty_patch_returns_empty_set() -> None:
    """Пустой patch → пустое множество (BEH-01)."""
    from disputatio.core.oscillation import _changed_lines

    assert _changed_lines("") == set()


def test_changed_lines_without_hunk_header_returns_empty_set() -> None:
    """Patch без заголовка `@@` → пустое множество, даже со строками `+`/`-` (BEH-01)."""
    from disputatio.core.oscillation import _changed_lines

    patch = "+addition without hunk\n-deletion without hunk\n"
    assert _changed_lines(patch) == set()


def test_oscillation_diff_threshold_is_pinned() -> None:
    """`OSCILLATION_DIFF_THRESHOLD == 0.8` (ADR-W2-02)."""
    from disputatio.core.oscillation import OSCILLATION_DIFF_THRESHOLD

    assert OSCILLATION_DIFF_THRESHOLD == 0.8


def test_claim_similarity_threshold_is_pinned() -> None:
    """`CLAIM_SIMILARITY_THRESHOLD == 0.7` (ADR-W2-03)."""
    from disputatio.core.oscillation import CLAIM_SIMILARITY_THRESHOLD

    assert CLAIM_SIMILARITY_THRESHOLD == 0.7


def test_patch_similarity_exactly_at_threshold_does_not_trigger() -> None:
    """Similarity ровно 0.8 не должна расцениваться как строгое `>` 0.8."""
    from disputatio.core.oscillation import OSCILLATION_DIFF_THRESHOLD, patch_similarity

    # A ⊂ B, |A|=4, |B|=5: |intersection|/|union| = 4/5 = 0.8.
    a = "@@ -1,4 +1,4 @@\n+one\n+two\n+three\n+four\n"
    b = "@@ -1,5 +1,5 @@\n+one\n+two\n+three\n+four\n+five\n"
    similarity = patch_similarity(a, b)

    assert similarity == 0.8
    assert not (similarity > OSCILLATION_DIFF_THRESHOLD)


def test_find_repeated_issue_third_occurrence_returns_issue() -> None:
    """Тот же file + нечётко совпадающий claim в 2 прошлых раундах → issue."""
    from disputatio.core.oscillation import find_repeated_issue

    current = [make_issue(claim="Off by one error in loop")]
    history = {
        1: [make_issue(claim="off by one error in loop")],
        2: [make_issue(claim="Off  by one  error in loop")],
    }

    result = find_repeated_issue(current, history)

    assert result is not None
    assert result.claim == "Off by one error in loop"


def test_find_repeated_issue_second_occurrence_returns_none() -> None:
    """Совпадение лишь в одном прошлом раунде (2-е открытие) → None."""
    from disputatio.core.oscillation import find_repeated_issue

    current = [make_issue(claim="Off by one error in loop")]
    history = {1: [make_issue(claim="off by one error in loop")]}

    assert find_repeated_issue(current, history) is None


def test_find_repeated_issue_different_file_returns_none() -> None:
    """Совпадающий claim, но другой file → не считается тем же issue."""
    from disputatio.core.oscillation import find_repeated_issue

    current = [make_issue(file="a.py", claim="Off by one error in loop")]
    history = {
        1: [make_issue(file="b.py", claim="off by one error in loop")],
        2: [make_issue(file="c.py", claim="off by one error in loop")],
    }

    assert find_repeated_issue(current, history) is None


def test_find_repeated_issue_matches_case_and_whitespace() -> None:
    """Регистр и схлопывание пробелов не мешают нечёткому совпадению."""
    from disputatio.core.oscillation import find_repeated_issue

    current = [make_issue(claim="  OFF BY ONE   error in loop  ")]
    history = {
        1: [make_issue(claim="off by one error in loop")],
        2: [make_issue(claim="Off By One Error In Loop")],
    }

    result = find_repeated_issue(current, history)

    assert result is not None


def test_find_repeated_issue_does_not_mutate_inputs() -> None:
    """Входные `Issue`/mapping не мутируются `find_repeated_issue`."""
    from disputatio.core.oscillation import find_repeated_issue

    current = [make_issue(claim="Off by one error in loop")]
    history = MappingProxyType(
        {
            1: [make_issue(claim="off by one error in loop")],
            2: [make_issue(claim="Off  by one  error in loop")],
        }
    )
    current_snapshot = list(current)
    history_snapshot = {k: list(v) for k, v in history.items()}

    find_repeated_issue(current, history)

    assert current == current_snapshot
    assert {k: list(v) for k, v in history.items()} == history_snapshot


def test_patch_similarity_does_not_mutate_arguments() -> None:
    """`patch_similarity` — чистая функция: аргументы-строки не меняются."""
    from disputatio.core.oscillation import patch_similarity

    a = "+alpha\n-beta\n"
    b = "+alpha\n-gamma\n"
    a_before, b_before = a, b

    patch_similarity(a, b)

    assert a == a_before
    assert b == b_before


def test_find_repeated_issue_empty_history_returns_none() -> None:
    """Без прошлых раундов совпадений быть не может."""
    from disputatio.core.oscillation import find_repeated_issue

    current = [make_issue()]

    assert find_repeated_issue(current, {}) is None


def test_changed_lines_tracks_consecutive_hunks() -> None:
    """BEH-03: новый заголовок `@@` продолжает разбор того же файла.

    Регрессионный тест по waiver-у TASK-003 (tdd-waiver/v1,
    spec/.tdd-evidence/waivers/a5b6a41bc40a1c96/TASK-003.json): поведение
    уже обеспечено state-машиной TASK-001/002, честный RED невозможен —
    тест закрепляет его зелёным, вне TDD-цикла.
    """
    from disputatio.core.oscillation import _changed_lines

    patch = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,2 +1,2 @@\n"
        "+first hunk line\n"
        "@@ -10,2 +10,2 @@\n"
        "+second hunk line\n"
        "-removed in second\n"
    )

    got = _changed_lines(patch)

    assert "first hunk line" in got
    assert "second hunk line" in got
    assert "removed in second" in got


def test_changed_lines_excludes_metadata_context_and_no_newline_marker() -> None:
    """BEH-04: служебные и контекстные строки исключаются (waiver TASK-004)."""
    from disputatio.core.oscillation import _changed_lines

    patch = (
        "diff --git a/f b/f\n"
        "index 1111111..2222222 100644\n"
        "old mode 100644\n"
        "--- a/f\n"
        "+++ b/f\n"
        "@@ -1,2 +1,2 @@\n"
        " context line\n"
        "+added\n"
        "\\ No newline at end of file\n"
    )

    assert _changed_lines(patch) == {"added"}


def test_changed_lines_normalizes_to_unique_content_set() -> None:
    """BEH-07: ровно один маркер + rstrip; регистр/пробелы сохранены,
    дубликаты схлопнуты, пустая строка допустима (waiver TASK-007)."""
    from disputatio.core.oscillation import _changed_lines

    patch = "@@ -1,4 +1,4 @@\n+  Keep  Inner \n+  Keep  Inner\n+\n-\n"

    assert _changed_lines(patch) == {"  Keep  Inner", ""}


def test_patch_similarity_ignores_all_service_line_differences() -> None:
    """BEH-08: пути, index, mode, заголовки и объём служебных строк не
    влияют на множество и похожесть (waiver TASK-008)."""
    from disputatio.core.oscillation import _changed_lines, patch_similarity

    a = (
        "diff --git a/x b/x\n"
        "index 1111111..2222222 100644\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "+same\n"
    )
    b = (
        "diff --git a/y b/y\n"
        "index 9999999..8888888 100755\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "--- a/y\n"
        "+++ b/y\n"
        "@@ -5,1 +5,1 @@\n"
        "+same\n"
    )
    third = "@@ -1,2 +1,2 @@\n+same\n-other\n"

    assert _changed_lines(a) == _changed_lines(b)
    assert patch_similarity(a, third) == patch_similarity(b, third)


def test_stateful_changed_lines_preserves_oscillation_contract() -> None:
    """BEH-09: формула Жаккара, случай двух пустых множеств и пороги
    детектора не изменены (waiver TASK-009)."""
    from disputatio.core.oscillation import (
        CLAIM_SIMILARITY_THRESHOLD,
        OSCILLATION_DIFF_THRESHOLD,
        patch_similarity,
    )

    assert patch_similarity("", "") == 1.0
    similarity = patch_similarity(
        "@@ -1,2 +1,2 @@\n+x\n+y\n", "@@ -1,2 +1,2 @@\n+y\n+z\n"
    )
    assert similarity == 1 / 3  # |{y}| / |{x, y, z}|
    assert OSCILLATION_DIFF_THRESHOLD == 0.8
    assert CLAIM_SIMILARITY_THRESHOLD == 0.7


def test_changed_lines_has_no_state_between_calls() -> None:
    """BEH-10: вызовы детерминированы и изолированы — состояние ханка не
    переносится, аргументы не мутируются (waiver TASK-010)."""
    from disputatio.core.oscillation import _changed_lines

    with_hunk = "@@ -1 +1 @@\n+q\n"
    without_hunk = "+no hunk\n"
    with_before = with_hunk

    first = _changed_lines(with_hunk)
    middle = _changed_lines(without_hunk)
    second = _changed_lines(with_hunk)

    assert first == second == {"q"}
    assert middle == set()
    assert with_hunk == with_before


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
