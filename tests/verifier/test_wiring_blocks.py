"""Разбор блоков правил и покрытия гейта wiring (design §3.1, §5.1, §5.3).

Спека несёт ровно один fenced-блок `disputatio-wiring` (TOML) с массивом
`rule`; план — ровно один блок `disputatio-wiring-cover` с `src_tree` и
массивом `cover`. Оба разбираются через `tomllib.loads` с последующей явной
сверкой множества ключей (закрытая схема) — неизвестный ключ, отсутствующее
обязательное поле и любое нарушение формы дают `WiringInputError` (код 2,
§6). Markdown читает `MarkdownPlanReader`; семантика разделов задач (§5.3)
проверяется рядом с ним — `tests/test_plan_markdown.py`.

Кросс-документная проверка (согласованность `member` с видом правила,
объявленным в спеке) — не эта задача, она в задаче 5: здесь `member`
только типизируется (строка или отсутствие).
"""

import pytest

from disputatio.plan_markdown import MarkdownPlanReader
from disputatio.verifier.wiring import (
    ConstructRule,
    Cover,
    CoverBlock,
    EnumerateRule,
    parse_cover,
    parse_rules,
)
from disputatio.verifier.wiring_snapshot import WiringInputError

READER = MarkdownPlanReader()

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
        rules = parse_rules(_spec(), reader=READER)

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
        cover_block = parse_cover(_plan(), reader=READER)

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
            parse_rules("документ без единого fenced-блока\n", reader=READER)

    def test_two_blocks_raises(self) -> None:
        text = (
            _wrap("disputatio-wiring", EXAMPLE_RULES_BODY)
            + "\n"
            + _wrap("disputatio-wiring", EXAMPLE_RULES_BODY)
        )

        with pytest.raises(WiringInputError):
            parse_rules(text, reader=READER)

    def test_toml_info_string_block_not_read(self) -> None:
        """Блок с info-строкой `toml` — не наш блок: он «не найден» (§3.1)."""
        with pytest.raises(WiringInputError):
            parse_rules(_spec(info="toml"), reader=READER)

    def test_nested_fenced_block_not_read(self) -> None:
        """Открывающая строка внутри уже открытого блока — литеральный текст."""
        outer = (
            "````markdown\n```disputatio-wiring\n" + EXAMPLE_RULES_BODY + "```\n````\n"
        )

        with pytest.raises(WiringInputError):
            parse_rules(outer, reader=READER)

    def test_broken_toml_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_rules(_spec(body="this is not [ valid toml\n"), reader=READER)


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
            parse_rules(_spec(body=body), reader=READER)

    def test_unknown_kind_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "constructs-everything"\n'
            'class = "pkg.mod:C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body), reader=READER)

    def test_missing_required_field_raises_for_construct_rule(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body), reader=READER)

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
            parse_rules(_spec(body=body), reader=READER)

    def test_empty_allowed_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
            "allowed = []\n"
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body), reader=READER)

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
            parse_rules(_spec(body=body), reader=READER)

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
            parse_rules(_spec(body=body), reader=READER)

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
            parse_rules(_spec(body=body), reader=READER)

    def test_id_outside_grammar_raises_for_uppercase(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "P10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body), reader=READER)

    def test_id_with_trailing_newline_raises(self) -> None:
        """`$` в `re.match` принимает `"r\\n"`; грамматика `id` — целиком (§3.1)."""
        body = (
            "[[rule]]\n"
            'id = "r\\n"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body), reader=READER)

    def test_class_without_colon_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod.C"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body), reader=READER)

    def test_class_with_dot_in_qualname_raises(self) -> None:
        body = (
            "[[rule]]\n"
            'id = "p10-policy"\n'
            'kind = "construct-only-in"\n'
            'class = "pkg.mod:Outer.Inner"\n'
            'allowed = ["src/pkg/mod.py"]\n'
        )

        with pytest.raises(WiringInputError):
            parse_rules(_spec(body=body), reader=READER)

    def test_empty_rule_array_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_rules(_spec(body="rule = []\n"), reader=READER)

    def test_missing_rule_key_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_rules(_spec(body='unrelated = "x"\n'), reader=READER)


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
            parse_rules(_spec(body=body), reader=READER)


class TestCoverSchema:
    def test_missing_src_tree_raises(self) -> None:
        body = '[[cover]]\nrule = "p10-policy"\nsite = "src/pkg/mod.py:1"\ntask = 1\n'

        with pytest.raises(WiringInputError):
            parse_cover(_plan(body=body), reader=READER)

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
            parse_cover(_plan(body=body), reader=READER)

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
            parse_cover(_plan(body=body), reader=READER)

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
            parse_cover(_plan(body=body), reader=READER)

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
            parse_cover(_plan(body=body), reader=READER)

    def test_two_blocks_raises(self) -> None:
        text = (
            _wrap("disputatio-wiring-cover", EXAMPLE_COVER_BODY)
            + "\n"
            + _wrap("disputatio-wiring-cover", EXAMPLE_COVER_BODY)
        )

        with pytest.raises(WiringInputError):
            parse_cover(text, reader=READER)

    def test_broken_toml_raises(self) -> None:
        with pytest.raises(WiringInputError):
            parse_cover(_plan(body="not [ valid\n"), reader=READER)
