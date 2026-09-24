"""Разбор блоков правил и покрытия гейта wiring (design §3.1, §5.1, §5.3).

Спека несёт ровно один fenced-блок `disputatio-wiring` (TOML) с массивом
`rule`; план — ровно один блок `disputatio-wiring-cover` с `src_tree` и
массивом `cover`. Оба разбираются через `tomllib.loads` с последующей явной
сверкой множества ключей (закрытая схема) — неизвестный ключ, отсутствующее
обязательное поле и любое нарушение формы дают `WiringInputError` (код 2,
§6). Раздельно проверяется `task_sections`: заголовки задач плана и границы
их разделов (§5.3).

Кросс-документная проверка (согласованность `member` с видом правила,
объявленным в спеке) — не эта задача, она в задаче 5: здесь `member`
только типизируется (строка или отсутствие).
"""

import pytest

from disputatio.verifier.wiring import (
    ConstructRule,
    Cover,
    CoverBlock,
    EnumerateRule,
    parse_cover,
    parse_rules,
    task_sections,
)
from disputatio.verifier.wiring_snapshot import WiringInputError

EXAMPLE_RULES_BODY = """\
[[rule]]
id = "p10-policy"
kind = "construct-only-in"
class = "disputatio.runtime.pipeline_runner:ArchitecturalDefectPolicy"
allowed = ["src/disputatio/runtime/composition.py"]

[[rule]]
id = "append-only-guard"
kind = "enumerates-all"
module = "src/disputatio/events/pipeline_store.py"
function = "_guard_history"
checkers = ["_guard_sessions", "_guard_immutable"]
members = ["spec_sessions", "pair_sessions", "doc_sessions",
           "transitions", "operator_decisions"]
"""

EXAMPLE_COVER_BODY = """\
src_tree = "70637371794b53ddc85dffb10624838e53ffb07c"

[[cover]]
rule = "p10-policy"
site = "src/disputatio/runtime/pipeline_runner.py:581"
task = 5

[[cover]]
rule = "append-only-guard"
site = "src/disputatio/events/pipeline_store.py:109"
member = "doc_sessions"
task = 1
"""


def _wrap(info: str, body: str) -> str:
    """Оборачивает `body` в fenced-блок с info-строкой `info` среди прочего текста."""
    return f"преамбула документа\n\n```{info}\n{body}```\n\nхвост документа\n"


def _spec(body: str = EXAMPLE_RULES_BODY, info: str = "disputatio-wiring") -> str:
    return _wrap(info, body)


def _plan(body: str = EXAMPLE_COVER_BODY, info: str = "disputatio-wiring-cover") -> str:
    return _wrap(info, body)


class TestParseRulesExample:
    def test_returns_two_rules_with_expected_fields(self) -> None:
        rules = parse_rules(_spec())

        assert rules == (
            ConstructRule(
                id="p10-policy",
                class_module="disputatio.runtime.pipeline_runner",
                class_name="ArchitecturalDefectPolicy",
                allowed=("src/disputatio/runtime/composition.py",),
            ),
            EnumerateRule(
                id="append-only-guard",
                module="src/disputatio/events/pipeline_store.py",
                function="_guard_history",
                checkers=("_guard_sessions", "_guard_immutable"),
                members=(
                    "spec_sessions",
                    "pair_sessions",
                    "doc_sessions",
                    "transitions",
                    "operator_decisions",
                ),
            ),
        )


class TestParseCoverExample:
    def test_returns_cover_block_with_expected_fields(self) -> None:
        cover_block = parse_cover(_plan())

        assert cover_block == CoverBlock(
            src_tree="70637371794b53ddc85dffb10624838e53ffb07c",
            covers=(
                Cover(
                    rule="p10-policy",
                    site="src/disputatio/runtime/pipeline_runner.py:581",
                    member=None,
                    task=5,
                ),
                Cover(
                    rule="append-only-guard",
                    site="src/disputatio/events/pipeline_store.py:109",
                    member="doc_sessions",
                    task=1,
                ),
            ),
        )


class TestRuleBlockDiscovery:
    def test_missing_block_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_rules("документ без единого fenced-блока\n")

    def test_two_blocks_raises(self) -> None:
        text = (
            _wrap("disputatio-wiring", EXAMPLE_RULES_BODY)
            + "\n"
            + _wrap("disputatio-wiring", EXAMPLE_RULES_BODY)
        )

        with pytest.raises(WiringInputError):
            parse_rules(text)

    def test_toml_info_string_block_not_read(self) -> None:
        """Блок с info-строкой `toml` — не наш блок: он «не найден» (§3.1)."""
        with pytest.raises(WiringInputError):
            parse_rules(_spec(info="toml"))

    def test_nested_fenced_block_not_read(self) -> None:
        """Открывающая строка внутри уже открытого блока — литеральный текст."""
        outer = (
            "````markdown\n```disputatio-wiring\n" + EXAMPLE_RULES_BODY + "```\n````\n"
        )

        with pytest.raises(WiringInputError):
            parse_rules(outer)

    def test_broken_toml_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_rules(_spec(body="this is not [ valid toml\n"))


class TestRuleSchema:
    def test_unknown_rule_key_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
            'extra = "not allowed"\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_unknown_kind_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "constructs-everything"\n'
            'class = "pkg.mod:C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_missing_required_field_raises_for_construct_rule(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_missing_required_field_raises_for_enumerate_rule(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "append-only-guard"\n'
            'kind = "enumerates-all"\n'
            'module = "src/pkg/mod.py"\n'
            'function = "_guard"\n'
            'checkers = ["_check"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_empty_allowed_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
            "allowed = []\n"
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_empty_checkers_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "append-only-guard"\n'
            'kind = "enumerates-all"\n'
            'module = "src/pkg/mod.py"\n'
            'function = "_guard"\n'
            "checkers = []\n"
            'members = ["a"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_empty_members_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "append-only-guard"\n'
            'kind = "enumerates-all"\n'
            'module = "src/pkg/mod.py"\n'
            'function = "_guard"\n'
            'checkers = ["_check"]\n'
            "members = []\n"
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_duplicate_id_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "dup"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
            "\n"
            "[[rule]]\n"
            'id = "dup"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:D"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_id_outside_grammar_raises_for_uppercase(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "P10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_class_without_colon_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod.C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_class_with_dot_in_qualname_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:Outer.Inner"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body))

    def test_empty_rule_array_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_rules(_spec(body="rule = []\n"))

    def test_missing_rule_key_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_rules(_spec(body='unrelated = "x"\n'))


class TestRuleListDuplicates:
    """Повтор элемента в `members`/`checkers`/`allowed` — код `2` (§3.1)."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ('"transitions", "operator_decisions"]', '"transitions", "transitions"]'),
            (
                '["_guard_sessions", "_guard_immutable"]',
                '["_guard_sessions", "_guard_sessions"]',
            ),
            (
                'allowed = ["src/disputatio/runtime/composition.py"]',
                (
                    'allowed = ["src/disputatio/runtime/composition.py", '
                    '"src/disputatio/runtime/composition.py"]'
                ),
            ),
        ],
        ids=["members", "checkers", "allowed"],
    )
    def test_duplicate_list_item_raises(self, old: str, new: str) -> None:
        assert old in EXAMPLE_RULES_BODY
        body = EXAMPLE_RULES_BODY.replace(old, new)

        with pytest.raises(WiringInputError, match="повтор"):
            parse_rules(_spec(body=body))


class TestCoverSchema:
    def test_missing_src_tree_raises(self) -> None:
        body = '[[cover]]\nrule = "p10-policy"\nsite = "src/pkg/mod.py:1"\ntask = 1\n'

        with pytest.raises(WiringInputError):
            parse_cover(_plan(body=body))

    def test_member_not_string_raises(self) -> None:
        body = (
            'src_tree = "abc123"\n'
            "\n"
            "[[cover]]\n"
            'rule = "append-only-guard"\n'
            'site = "src/pkg/mod.py:1"\n'
            "member = 1\n"
            "task = 1\n"
        )

        with pytest.raises(WiringInputError):
            parse_cover(_plan(body=body))

    def test_task_less_than_one_raises(self) -> None:
        body = (
            'src_tree = "abc123"\n'
            "\n"
            "[[cover]]\n"
            'rule = "p10-policy"\n'
            'site = "src/pkg/mod.py:1"\n'
            "task = 0\n"
        )

        with pytest.raises(WiringInputError):
            parse_cover(_plan(body=body))

    def test_task_not_integer_raises(self) -> None:
        body = (
            'src_tree = "abc123"\n'
            "\n"
            "[[cover]]\n"
            'rule = "p10-policy"\n'
            'site = "src/pkg/mod.py:1"\n'
            "task = 1.5\n"
        )

        with pytest.raises(WiringInputError):
            parse_cover(_plan(body=body))

    def test_unknown_cover_key_raises(self) -> None:
        body = (
            'src_tree = "abc123"\n'
            "\n"
            "[[cover]]\n"
            'rule = "p10-policy"\n'
            'site = "src/pkg/mod.py:1"\n'
            "task = 1\n"
            'extra = "nope"\n'
        )

        with pytest.raises(WiringInputError):
            parse_cover(_plan(body=body))

    def test_two_blocks_raises(self) -> None:
        text = (
            _wrap("disputatio-wiring-cover", EXAMPLE_COVER_BODY)
            + "\n"
            + _wrap("disputatio-wiring-cover", EXAMPLE_COVER_BODY)
        )

        with pytest.raises(WiringInputError):
            parse_cover(text)

    def test_broken_toml_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_cover(_plan(body="not [ valid\n"))


class TestTaskSections:
    def test_recognizes_both_heading_styles(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "текст первой задачи\n"
            "### Task 2: покрытие\n"
            "текст второй задачи\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1, 2}
        assert "текст первой задачи" in sections[1]
        assert "текст второй задачи" in sections[2]

    def test_section_ends_at_next_heading_level_1_to_3(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "тело задачи 1\n"
            "## Другой раздел плана\n"
            "тело задачи 1 сюда уже не входит\n"
        )

        sections = task_sections(text)

        assert "тело задачи 1" in sections[1]
        assert "тело задачи 1 сюда уже не входит" not in sections[1]

    def test_level_4_heading_does_not_end_section(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "тело задачи 1\n"
            "#### Подраздел\n"
            "всё ещё тело задачи 1\n"
        )

        sections = task_sections(text)

        assert "всё ещё тело задачи 1" in sections[1]

    def test_level_2_heading_is_not_task_heading_but_ends_section(self) -> None:
        """`## Task N:` не заголовок задачи (§5.3: только `###`), но обрывает раздел."""
        text = (
            "### Задача 1: разбор\n"
            "тело задачи 1\n"
            "## Task 3: не заголовок задачи\n"
            "текст после заголовка второго уровня\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "тело задачи 1" in sections[1]
        assert "текст после заголовка второго уровня" not in sections[1]

    def test_level_1_heading_is_not_task_heading_but_ends_section(self) -> None:
        """`# Задача N:` — не заголовок задачи (только `###`), но обрывает раздел."""
        text = (
            "### Задача 1: разбор\n"
            "тело задачи 1\n"
            "# Задача 3: не заголовок задачи\n"
            "текст после заголовка первого уровня\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "тело задачи 1" in sections[1]
        assert "текст после заголовка первого уровня" not in sections[1]

    def test_non_level_3_task_like_heading_does_not_trigger_duplicate(self) -> None:
        """`## Task 3:` рядом с настоящей `### Задача 3:` — не дубль номера."""
        text = (
            "### Задача 3: настоящая\n"
            "текст настоящей задачи 3\n"
            "## Task 3: похоже на задачу, но не она\n"
            "текст после\n"
        )

        sections = task_sections(text)

        assert set(sections) == {3}
        assert "текст настоящей задачи 3" in sections[3]
        assert "текст после" not in sections[3]

    def test_duplicate_task_number_raises(self) -> None:
        text = "### Задача 1: разбор\nтекст\n### Задача 1: снова\nтекст\n"

        with pytest.raises(WiringInputError):
            task_sections(text)


class TestTaskSectionsFences:
    """Fenced-содержимое не входит в текст раздела и не даёт заголовков (§5.3).

    Один сканер фенсов на извлечение блоков и на разделы задач: иначе блок
    покрытия внутри раздела «ссылался» бы сам на все свои места.
    """

    def test_fenced_content_excluded_from_section_text(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "прозаический текст\n"
            "```python\n"
            "код внутри фенса\n"
            "```\n"
            "проза после фенса\n"
        )

        sections = task_sections(text)

        assert "прозаический текст" in sections[1]
        assert "проза после фенса" in sections[1]
        assert "код внутри фенса" not in sections[1]

    def test_cover_block_content_excluded_from_section_text(self) -> None:
        text = (
            "### Task 1: unrelated work\n"
            "```disputatio-wiring-cover\n"
            'site = "src/pkg/b.py:8"\n'
            "```\n"
        )

        sections = task_sections(text)

        assert "src/pkg/b.py:8" not in sections[1]

    def test_task_heading_inside_fence_is_not_heading(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "```markdown\n"
            "### Task 2: пример заголовка в коде\n"
            "```\n"
            "проза задачи 1 после фенса\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "проза задачи 1 после фенса" in sections[1]

    def test_shorter_fence_line_does_not_close_longer_fence(self) -> None:
        """```` открыт — строка ``` внутри его не закрывает (CommonMark)."""
        text = (
            "### Задача 1: разбор\n"
            "````markdown\n"
            "```\n"
            "### Task 2: всё ещё внутри фенса\n"
            "внутри фенса\n"
            "```\n"
            "````\n"
            "проза после\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "внутри фенса" not in sections[1]
        assert "проза после" in sections[1]

    def test_fence_line_with_info_does_not_close_fence(self) -> None:
        """Закрывающая строка — только из кавычек: ```` ```x ```` не закрывает."""
        text = (
            "### Задача 1: разбор\n"
            "```\n"
            "```python\n"
            "### Task 2: всё ещё внутри фенса\n"
            "```\n"
            "проза после\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "проза после" in sections[1]

    def test_tilde_fence_hides_heading_and_content(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "~~~\n"
            "### Task 2: внутри тильдового фенса\n"
            "```\n"
            "тильдовое содержимое\n"
            "~~~\n"
            "проза после\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "тильдовое содержимое" not in sections[1]
        assert "проза после" in sections[1]

    def test_inline_code_at_line_start_is_not_fence(self) -> None:
        """```` ```x``` ```` в начале строки — инлайн-код, фенс не открывается."""
        text = (
            "### Задача 1: разбор\n"
            "```inline``` проза задачи 1\n"
            "### Task 2: настоящий заголовок\n"
            "проза задачи 2\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1, 2}
        assert "проза задачи 1" in sections[1]


class TestFenceIndentation:
    """Строка фенса — не более 3 пробелов отступа (CommonMark), как §5.3.

    Без этого ограничения строка примера с отступом 4+ (частый случай —
    иллюстрация фенса внутри фенса) закрывала бы или открывала бы блок
    раньше времени, и код/заголовок внутри примера утекал бы в прозу.
    """

    def test_four_space_indent_does_not_close_fence(self) -> None:
        text = (
            "### Задача 1: разбор\n```text\n    ```\nвнутри фенса\n```\nпроза после\n"
        )

        sections = task_sections(text)

        assert "внутри фенса" not in sections[1]
        assert "проза после" in sections[1]

    def test_four_space_indent_does_not_open_fence(self) -> None:
        text = "### Задача 1: разбор\n    ```\nпроза с отступом 4 остаётся прозой\n"

        sections = task_sections(text)

        assert "проза с отступом 4 остаётся прозой" in sections[1]

    def test_tab_indent_is_not_a_fence_line(self) -> None:
        text = "### Задача 1: разбор\n\t```\nпроза после табуляции\n"

        sections = task_sections(text)

        assert "проза после табуляции" in sections[1]

    @pytest.mark.parametrize("indent", ["", " ", "  ", "   "], ids=["0", "1", "2", "3"])
    def test_indent_zero_to_three_still_opens_and_closes(self, indent: str) -> None:
        text = (
            "### Задача 1: разбор\n"
            f"{indent}```python\n"
            "код внутри фенса\n"
            f"{indent}```\n"
            "проза после\n"
        )

        sections = task_sections(text)

        assert "код внутри фенса" not in sections[1]
        assert "проза после" in sections[1]


class TestCommonMarkHeadingBoundaries:
    """Граница раздела — любой заголовок 1–3 уровня по CommonMark (§5.3).

    Заголовок, который рендерится как заголовок, но не обрывает раздел,
    засчитал бы место под ним предыдущей задаче — тихий пропуск (код 0).
    """

    @pytest.mark.parametrize(
        "heading",
        [
            pytest.param("##  Примечания", id="two-spaces"),
            pytest.param("   ## Примечания", id="indent-3"),
            pytest.param(" # Примечания", id="indent-1-level-1"),
            pytest.param("##\tПримечания", id="tab"),
            pytest.param("##", id="bare-hashes"),
            pytest.param("## ", id="hashes-and-space"),
            pytest.param("Примечания\n===", id="setext-level-1"),
            pytest.param("Примечания\n---", id="setext-level-2"),
            pytest.param("Примечания\n  ----  ", id="setext-indented-trailing"),
        ],
    )
    def test_heading_form_ends_task_section(self, heading: str) -> None:
        text = (
            f"### Задача 1: разбор\nтело задачи 1\n\n{heading}\nхвост после заголовка\n"
        )

        sections = task_sections(text)

        assert "тело задачи 1" in sections[1]
        assert "хвост после заголовка" not in sections[1]

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("####  Подраздел", id="level-4"),
            pytest.param("#5 без пробела", id="no-space"),
            pytest.param("    ## отступ 4 — код", id="indent-4"),
        ],
    )
    def test_non_heading_does_not_end_section(self, line: str) -> None:
        text = f"### Задача 1: разбор\nтело задачи 1\n{line}\nвсё ещё тело\n"

        sections = task_sections(text)

        assert "всё ещё тело" in sections[1]

    def test_thematic_break_after_blank_line_is_not_boundary(self) -> None:
        text = "### Задача 1: разбор\nтело задачи 1\n\n---\nвсё ещё тело задачи 1\n"

        sections = task_sections(text)

        assert "всё ещё тело задачи 1" in sections[1]

    def test_setext_underline_inside_fence_is_not_boundary(self) -> None:
        text = "### Задача 1: разбор\n```\nПример\n===\n```\nпроза задачи 1\n"

        sections = task_sections(text)

        assert "проза задачи 1" in sections[1]

    def test_setext_task_like_heading_is_boundary_not_task(self) -> None:
        text = "### Задача 1: разбор\nтело задачи 1\nTask 2: текст\n---\nхвост\n"

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "хвост" not in sections[1]

    @pytest.mark.parametrize(
        "heading",
        [
            pytest.param("###  Задача 3:  разбор", id="two-spaces"),
            pytest.param("  ### Задача 3: разбор", id="indent-2"),
            pytest.param("###\tTask\t3\t: разбор", id="tabs"),
        ],
    )
    def test_task_heading_whitespace_variants_still_task(self, heading: str) -> None:
        text = f"{heading}\nтело задачи 3\n"

        sections = task_sections(text)

        assert set(sections) == {3}
        assert "тело задачи 3" in sections[3]


class TestFenceScannerForBlocks:
    def test_block_closed_only_by_fence_of_same_length_or_longer(self) -> None:
        """Блок в ````-фенсе: строка ``` внутри — литерал, а не конец блока."""
        body = 'src_tree = """\n```\n"""\n'
        text = f"````disputatio-wiring-cover\n{body}````\n"

        block = parse_cover(text)

        assert block.src_tree == "```\n"

    def test_block_inside_tilde_fence_not_read(self) -> None:
        """Блок внутри `~~~`-фенса — литеральный текст примера, не блок."""
        text = "~~~\n```disputatio-wiring\n" + EXAMPLE_RULES_BODY + "```\n~~~\n"

        with pytest.raises(WiringInputError):
            parse_rules(text)


class TestTaskSectionsHtmlComments:
    """HTML-комментарий вне фенса не рендерится — не заголовок и не текст (§5.3).

    Комментарий, блочный или встроенный, вырезается до поиска заголовков и
    сборки текста раздела; незакрытый `<!--` тянется до конца документа
    (CommonMark, HTML-блок типа 2). Иначе невидимый `### Task N:` создавал бы
    задачу, а невидимое место засчитывалось бы ссылкой — тихий код 0.
    """

    def test_task_heading_inside_block_comment_is_not_task(self) -> None:
        text = "<!--\n### Task 1: скрытая\nsrc/pkg/guard.py:1 b\n-->\n"

        assert task_sections(text) == {}

    def test_unclosed_comment_hides_rest_of_document(self) -> None:
        text = "### Задача 1: видимая\nтело\n<!--\n### Task 2: скрытая\nsrc/x.py:1\n"

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "src/x.py:1" not in sections[1]

    def test_site_inside_comment_is_not_section_text(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "видимый текст <!-- src/pkg/a.py:3 --> и хвост\n"
            "<!--\nsrc/pkg/b.py:4\n-->\n"
            "после комментария\n"
        )

        section = task_sections(text)[1]

        assert "src/pkg/a.py:3" not in section
        assert "src/pkg/b.py:4" not in section
        assert "видимый текст" in section
        assert "и хвост" in section
        assert "после комментария" in section

    def test_hidden_heading_is_not_a_boundary(self) -> None:
        """Невидимый заголовок раздел не обрывает: текст под ним — задачи 1."""
        text = "### Задача 1: разбор\n<!--\n## Скрыто\n-->\nтекст задачи 1\n"

        assert "текст задачи 1" in task_sections(text)[1]

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("<!-- x -->### Task 2: мнимая", id="after-inline"),
            pytest.param("<!-- x --> ### Task 2: мнимая", id="after-inline-space"),
            pytest.param("   <!-- x -->### Task 2: мнимая", id="indented"),
            pytest.param("<!---->### Task 2: мнимая", id="empty-comment"),
            pytest.param("<!-->### Task 2: мнимая", id="short-comment"),
        ],
    )
    def test_line_starting_with_comment_is_not_heading(self, line: str) -> None:
        """Строка, начатая `<!--`, — HTML-блок до конца строки, не заголовок."""
        text = f"### Задача 1: разбор\n{line}\n"

        assert set(task_sections(text)) == {1}

    @pytest.mark.parametrize("comment", ["<!-->", "<!--->", "<!---->"])
    def test_short_comment_closes_immediately(self, comment: str) -> None:
        """`<!-->` и `<!--->` — законченные комментарии (CommonMark)."""
        text = f"### Задача 1: разбор\n{comment}\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in task_sections(text)[1]

    def test_line_ending_comment_is_not_heading(self) -> None:
        text = "### Задача 1: разбор\n<!--\nскрыто\n--> ### Task 2: мнимая\n"

        assert set(task_sections(text)) == {1}

    def test_heading_with_trailing_inline_comment_is_task(self) -> None:
        text = "### Task 3: разбор <!-- заметка -->\nтело задачи 3\n"

        sections = task_sections(text)

        assert set(sections) == {3}
        assert "заметка" not in sections[3]

    def test_comment_marker_inside_fence_is_literal(self) -> None:
        text = "### Задача 1: разбор\n```html\n<!--\n```\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in task_sections(text)[1]

    def test_fence_inside_comment_is_not_block(self) -> None:
        """Единственный блок внутри комментария — не блок: код 2."""
        text = "<!--\n```disputatio-wiring\n" + EXAMPLE_RULES_BODY + "```\n-->\n"

        with pytest.raises(WiringInputError, match="не найден"):
            parse_rules(text)

    def test_fence_inside_comment_does_not_hide_following_text(self) -> None:
        """Открытая в комментарии «ограда» фенсом не считается и прозу не ест."""
        text = "### Задача 1: разбор\n<!--\n```\n-->\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in task_sections(text)[1]


class TestTaskSectionsRawHtml:
    """Сырые HTML-блоки плана: гейт не видит в них больше, чем рендер (§5.3).

    `<script>`/`<style>`/`<pre>`/`<textarea>` (HTML-блок типа 1) —
    сырой текст до закрывающего тега: не заголовки и не текст раздела.
    Прочие HTML-блоки (типы 6–7) тянутся до пустой строки: markdown в них не
    разбирается, поэтому строка `### Task N:` в них задачей не становится
    (но раздел обрывает — сомнение в сторону сужения).
    """

    @pytest.mark.parametrize("tag", ["script", "style", "pre", "textarea", "PRE"])
    def test_type1_block_hides_heading_and_text(self, tag: str) -> None:
        text = (
            "### Задача 1: разбор\nвидимо\n"
            f"<{tag}>\n### Task 2: скрытая\nsrc/pkg/a.py:3\n</{tag}> хвост\n"
            "после блока\n"
        )

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "src/pkg/a.py:3" not in sections[1]
        assert "хвост" not in sections[1]
        assert "после блока" in sections[1]

    def test_unclosed_type1_block_hides_rest(self) -> None:
        text = "<script>\n### Task 1: скрытая\nsrc/pkg/a.py:3\n"

        assert task_sections(text) == {}

    def test_type1_block_closes_on_same_line(self) -> None:
        text = "### Задача 1: разбор\n<pre>x</pre>\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in task_sections(text)[1]

    @pytest.mark.parametrize("tag", ["div", "details", "span", "/div", "table"])
    def test_task_heading_inside_html_block_is_not_task(self, tag: str) -> None:
        text = f"<{tag}>\n### Task 1: не заголовок\nsrc/pkg/a.py:3\n"

        assert task_sections(text) == {}

    def test_html_block_line_still_ends_previous_section(self) -> None:
        text = "### Задача 1: разбор\n\n<div>\n### Task 2: x\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" not in task_sections(text)[1]

    def test_html_block_ends_at_blank_line(self) -> None:
        text = "<details>\n<summary>x</summary>\n\n### Task 1: видимая\nтело\n"

        assert set(task_sections(text)) == {1}

    def test_comment_line_does_not_end_html_block(self) -> None:
        """Строка-комментарий внутри HTML-блока — не пустая строка блока."""
        text = "<div>\n<!-- c -->\n### Task 1: не заголовок\n"

        assert task_sections(text) == {}


class TestTaskSectionsLinkReferenceDefinitions:
    """Определение ссылки `[x]: …` не рендерится — не текст раздела (§5.3)."""

    @pytest.mark.parametrize(
        "definition",
        [
            pytest.param("[n]: src/pkg/a.py:3", id="plain"),
            pytest.param("   [n]: src/pkg/a.py:3", id="indent-3"),
            pytest.param("[^1]: src/pkg/a.py:3", id="footnote"),
            pytest.param("[n]:\n  src/pkg/a.py:3", id="destination-next-line"),
            pytest.param('[n]: /u\n  "src/pkg/a.py:3"', id="title-next-line"),
        ],
    )
    def test_definition_text_excluded(self, definition: str) -> None:
        text = f"### Задача 1: разбор\nвидимо\n\n{definition}\n\nпосле\n"

        section = task_sections(text)[1]

        assert "src/pkg/a.py:3" not in section
        assert "видимо" in section
        assert "после" in section

    def test_heading_after_definition_still_heading(self) -> None:
        text = "### Задача 1: разбор\n[n]: /u\n### Task 2: вторая\nтело 2\n"

        sections = task_sections(text)

        assert set(sections) == {1, 2}
        assert "тело 2" in sections[2]


class TestTaskSectionsContainerHeadings:
    """Заголовок в цитате или пункте списка обрывает раздел, но не задача (§5.3)."""

    @pytest.mark.parametrize(
        "heading",
        [
            pytest.param("> ### Task 2: в цитате", id="quote"),
            pytest.param(">## Примечания", id="quote-no-space"),
            pytest.param("> > # Глубже", id="nested-quote"),
            pytest.param("- ### Пункт", id="bullet"),
            pytest.param("* ## Пункт", id="star"),
            pytest.param("1. # Пункт", id="ordered"),
            pytest.param("      - ### Вложенный", id="nested-list"),
            pytest.param("> Примечания\n> ---", id="setext-in-quote"),
        ],
    )
    def test_container_heading_ends_section(self, heading: str) -> None:
        # Пустая строка перед заголовком: абзац setext без неё поднимается
        # по всем непустым строкам выше — сужение раздела (fail-closed).
        text = f"### Задача 1: разбор\nтело\n\n{heading}\nsrc/pkg/a.py:3\n"

        sections = task_sections(text)

        assert set(sections) == {1}
        assert "тело" in sections[1]
        assert "src/pkg/a.py:3" not in sections[1]

    @pytest.mark.parametrize(
        "line", ["- #123 задача", "> #5 без пробела", "- пункт ### не в начале"]
    )
    def test_container_non_heading_does_not_end_section(self, line: str) -> None:
        text = f"### Задача 1: разбор\n{line}\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in task_sections(text)[1]


class TestTaskHeadingNumber:
    def test_non_ascii_digits_are_not_task_number(self) -> None:
        """`\\d` Python матчит `١`; номер задачи — только ASCII-цифры."""
        text = "### Task ١: разбор\nsrc/pkg/a.py:3\n"

        assert task_sections(text) == {}

    def test_escaped_heading_is_not_task(self) -> None:
        assert task_sections("\\### Task 1: разбор\nтело\n") == {}

    def test_closing_hashes_keep_task_heading(self) -> None:
        assert set(task_sections("### Task 1: разбор ###\nтело\n")) == {1}
