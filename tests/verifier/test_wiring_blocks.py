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
