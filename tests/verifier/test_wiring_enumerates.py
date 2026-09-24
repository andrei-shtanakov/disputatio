"""Правило `enumerates-all` гейта wiring (design §3.5).

Правило проверяет **фактический перебор**, а не присутствие имён: тело
функции обязано иметь узкую форму — необязательный докстринг первым
оператором, затем только проверяющие вызовы `ast.Expr(ast.Call)` с `func`
из `checkers`. Член проверяющего вызова — `attr` его аргументов вида
`параметр.attr`; вызов обязан нести хотя бы один такой аргумент, и все они
обязаны нести один и тот же `attr`. Любая другая форма оператора —
`Unverifiable`, а не молчаливый успех.

Деревья синтетические, как в `test_wiring_resolve.py`: `Snapshot` собирается
прямо из словаря «путь → текст».
"""

from collections.abc import Mapping

import pytest

from disputatio.verifier.wiring import (
    EnumerateRule,
    ModuleIndex,
    Unverifiable,
    Violation,
    build_index,
    enumerate_violations,
)
from disputatio.verifier.wiring_snapshot import Snapshot, WiringInputError

MODULE_PATH = "src/m.py"

MEMBERS = (
    "spec_sessions",
    "pair_sessions",
    "doc_sessions",
    "transitions",
    "operator_decisions",
)


def _index(files: Mapping[str, str]) -> ModuleIndex:
    """Индекс синтетического снимка под `src`."""
    encoded = {path: text.encode("utf-8") for path, text in files.items()}
    return build_index(Snapshot(tree="0" * 40, files=encoded), "src")


def _rule(
    module: str = MODULE_PATH,
    function: str = "_guard_history",
    checkers: tuple[str, ...] = ("_guard_sessions", "_guard_immutable"),
    members: tuple[str, ...] = MEMBERS,
) -> EnumerateRule:
    return EnumerateRule(
        id="r", module=module, function=function, checkers=checkers, members=members
    )


def _missing(index: ModuleIndex, rule: EnumerateRule) -> list[Violation]:
    result = enumerate_violations(index, rule)
    assert isinstance(result, list)
    return result


# --- историческая форма `_guard_history` (снимок 23c9297) --------------------

# Воспроизведена синтетически, дословная форма из §3.1: докстринг, три
# однострочных проверяющих вызова и один многострочный.
GUARD_HISTORY = '''\
def _guard_history(previous: PipelineState, current: PipelineState) -> None:
    """Сверяет все четыре append-only коллекции манифеста (§4.2)."""
    _guard_sessions(previous.spec_sessions, current.spec_sessions, "spec_sessions")
    _guard_sessions(previous.pair_sessions, current.pair_sessions, "pair_sessions")
    _guard_immutable(previous.transitions, current.transitions, "transitions")
    _guard_immutable(
        previous.operator_decisions, current.operator_decisions, "operator_decisions"
    )
'''


def test_historical_guard_history_misses_doc_sessions() -> None:
    index = _index({MODULE_PATH: GUARD_HISTORY})
    assert _missing(index, _rule()) == [
        Violation(
            rule="r",
            kind="enumerates-all",
            site=f"{MODULE_PATH}:1",
            member="doc_sessions",
            count=1,
        )
    ]


ALL_CHECKED = """\
def _guard_history(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions)
    _guard_sessions(previous.pair_sessions, current.pair_sessions)
    _guard_sessions(previous.doc_sessions, current.doc_sessions)
    _guard_immutable(previous.transitions, current.transitions)
    _guard_immutable(previous.operator_decisions, current.operator_decisions)
"""


def test_all_members_checked_gives_no_violations() -> None:
    index = _index({MODULE_PATH: ALL_CHECKED})
    assert _missing(index, _rule()) == []


EXTRA_CHECKED = (
    ALL_CHECKED + "    _guard_sessions(previous.extra_field, current.extra_field)\n"
)


def test_extra_checked_member_is_not_a_violation() -> None:
    index = _index({MODULE_PATH: EXTRA_CHECKED})
    assert _missing(index, _rule()) == []


# --- член не засчитан, но форма поддерживается --------------------------------

DOCSTRING_ONLY = '''\
def _guard_history(previous, current) -> None:
    """Также проверяет doc_sessions между раундами."""
    _guard_sessions(previous.spec_sessions, current.spec_sessions)
    _guard_sessions(previous.pair_sessions, current.pair_sessions)
    _guard_immutable(previous.transitions, current.transitions)
    _guard_immutable(previous.operator_decisions, current.operator_decisions)
'''


def test_member_name_only_in_docstring_is_not_counted() -> None:
    index = _index({MODULE_PATH: DOCSTRING_ONLY})
    assert _missing(index, _rule()) == [
        Violation(
            rule="r",
            kind="enumerates-all",
            site=f"{MODULE_PATH}:1",
            member="doc_sessions",
            count=1,
        )
    ]


STRING_ARG_ONLY = """\
def _guard_history(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions, "doc_sessions")
    _guard_sessions(previous.pair_sessions, current.pair_sessions)
    _guard_immutable(previous.transitions, current.transitions)
    _guard_immutable(previous.operator_decisions, current.operator_decisions)
"""


def test_member_name_only_as_string_argument_is_not_counted() -> None:
    index = _index({MODULE_PATH: STRING_ARG_ONLY})
    assert _missing(index, _rule()) == [
        Violation(
            rule="r",
            kind="enumerates-all",
            site=f"{MODULE_PATH}:1",
            member="doc_sessions",
            count=1,
        )
    ]


# --- член только под неподдерживаемым оператором → Unverifiable целиком ------

NESTED_FUNCTION = """\
def _guard_history(previous, current) -> None:
    def _check_doc_sessions() -> None:
        _guard_sessions(previous.doc_sessions, current.doc_sessions)

    _guard_sessions(previous.spec_sessions, current.spec_sessions)
    _guard_sessions(previous.pair_sessions, current.pair_sessions)
    _guard_immutable(previous.transitions, current.transitions)
    _guard_immutable(previous.operator_decisions, current.operator_decisions)
"""


def test_member_name_only_in_nested_function_is_unverifiable() -> None:
    index = _index({MODULE_PATH: NESTED_FUNCTION})
    result = enumerate_violations(index, _rule())
    assert isinstance(result, Unverifiable)
    assert result.rule == "r"
    assert result.site == f"{MODULE_PATH}:1"


UNDER_IF = """\
def _guard_history(previous, current) -> None:
    if previous.doc_sessions != current.doc_sessions:
        _guard_sessions(previous.doc_sessions, current.doc_sessions)
    _guard_sessions(previous.spec_sessions, current.spec_sessions)
    _guard_sessions(previous.pair_sessions, current.pair_sessions)
    _guard_immutable(previous.transitions, current.transitions)
    _guard_immutable(previous.operator_decisions, current.operator_decisions)
"""


def test_member_name_only_under_if_is_unverifiable() -> None:
    index = _index({MODULE_PATH: UNDER_IF})
    result = enumerate_violations(index, _rule())
    assert isinstance(result, Unverifiable)
    assert result.site == f"{MODULE_PATH}:1"


# --- `Unverifiable`: прочие неподдерживаемые формы тела -----------------------

FOR_LOOP_FORM = """\
def _guard_history(previous, current) -> None:
    for member in ("spec_sessions", "pair_sessions", "doc_sessions"):
        _guard_sessions(getattr(previous, member), getattr(current, member))
    _guard_immutable(previous.transitions, current.transitions)
    _guard_immutable(previous.operator_decisions, current.operator_decisions)
"""

ASSIGNMENT_FORM = """\
def _guard_history(previous, current) -> None:
    ok = True
    _guard_sessions(previous.spec_sessions, current.spec_sessions)
"""

RETURN_FORM = """\
def _guard_history(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions)
    return None
"""

OTHER_CALL_FORM = """\
def _guard_history(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions)
    _log(previous.pair_sessions)
"""

NO_ATTR_ARG_FORM = """\
def _guard_history(previous, current) -> None:
    _guard_sessions("spec_sessions", "pair_sessions")
"""

TWO_ATTRS_FORM = """\
def _guard_history(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.pair_sessions)
"""


@pytest.mark.parametrize(
    "body",
    [
        FOR_LOOP_FORM,
        ASSIGNMENT_FORM,
        RETURN_FORM,
        OTHER_CALL_FORM,
        NO_ATTR_ARG_FORM,
        TWO_ATTRS_FORM,
    ],
)
def test_unsupported_body_forms_are_unverifiable(body: str) -> None:
    index = _index({MODULE_PATH: body})
    result = enumerate_violations(index, _rule())
    assert isinstance(result, Unverifiable)
    assert result.rule == "r"
    assert result.site == f"{MODULE_PATH}:1"


# --- разрешение qualname и коды 2 ----------------------------------------------

CLASS_METHOD = """\
class Guard:
    def check(self, previous, current) -> None:
        _guard_sessions(previous.spec_sessions, current.spec_sessions)
        _guard_sessions(previous.pair_sessions, current.pair_sessions)
        _guard_sessions(previous.doc_sessions, current.doc_sessions)
        _guard_immutable(previous.transitions, current.transitions)
        _guard_immutable(previous.operator_decisions, current.operator_decisions)
"""


def test_class_method_found_by_qualname() -> None:
    index = _index({MODULE_PATH: CLASS_METHOD})
    assert _missing(index, _rule(function="Guard.check")) == []


def test_function_not_found_is_input_error() -> None:
    index = _index({MODULE_PATH: ALL_CHECKED})
    with pytest.raises(WiringInputError, match="missing_fn"):
        enumerate_violations(index, _rule(function="missing_fn"))


def test_module_not_in_snapshot_is_input_error() -> None:
    index = _index({MODULE_PATH: ALL_CHECKED})
    with pytest.raises(WiringInputError, match="src/gone.py"):
        enumerate_violations(index, _rule(module="src/gone.py"))


def test_class_not_found_is_input_error() -> None:
    index = _index({MODULE_PATH: CLASS_METHOD})
    with pytest.raises(WiringInputError, match="Missing"):
        enumerate_violations(index, _rule(function="Missing.check"))
