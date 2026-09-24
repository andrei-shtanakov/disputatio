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

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from disputatio.cli import EXIT_FAILED, main
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


# --- неоднозначное определение — тоже код 2 -------------------------------

DUPLICATE_FUNCTION = """\
def guard(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions)


def guard(previous, current) -> None:
    _guard_sessions(previous.pair_sessions, current.pair_sessions)
"""


def test_duplicate_module_level_function_is_input_error() -> None:
    index = _index({MODULE_PATH: DUPLICATE_FUNCTION})
    with pytest.raises(WiringInputError, match="guard"):
        enumerate_violations(index, _rule(function="guard"))


DUPLICATE_METHOD = """\
class Guard:
    def check(self, previous, current) -> None:
        _guard_sessions(previous.spec_sessions, current.spec_sessions)

    def check(self, previous, current) -> None:
        _guard_sessions(previous.pair_sessions, current.pair_sessions)
"""


def test_duplicate_method_in_class_is_input_error() -> None:
    index = _index({MODULE_PATH: DUPLICATE_METHOD})
    with pytest.raises(WiringInputError, match="Guard.check"):
        enumerate_violations(index, _rule(function="Guard.check"))


DUPLICATE_CLASS = """\
class Guard:
    def check(self, previous, current) -> None:
        _guard_sessions(previous.spec_sessions, current.spec_sessions)


class Guard:
    def check(self, previous, current) -> None:
        _guard_sessions(previous.pair_sessions, current.pair_sessions)
"""


def test_duplicate_top_level_class_is_input_error() -> None:
    index = _index({MODULE_PATH: DUPLICATE_CLASS})
    with pytest.raises(WiringInputError, match="Guard"):
        enumerate_violations(index, _rule(function="Guard.check"))


DUPLICATE_FUNCTION_ASYNC = """\
def guard(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions)


async def guard(previous, current) -> None:
    _guard_sessions(previous.pair_sessions, current.pair_sessions)
"""


def test_def_and_async_def_same_name_is_input_error() -> None:
    index = _index({MODULE_PATH: DUPLICATE_FUNCTION_ASYNC})
    with pytest.raises(WiringInputError, match="guard"):
        enumerate_violations(index, _rule(function="guard"))


# --- условное определение и перепривязка имени — тоже код 2 -----------------

# Python связывает то определение, которое выполнилось последним; условное
# определение (под `if`/`try`/`with`/циклом/`match`) гейт статически не
# разрешает, поэтому любое определение или перепривязка искомого имени вне
# простой формы `def` верхнего уровня области — код 2, а не молчаливый
# разбор первого `def`.

PLAIN_GUARD = """\
def guard(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions)
"""

GUARD_ONLY = ("spec_sessions",)

CONDITIONAL_REDEFINITIONS = {
    "if": "if True:\n    def guard(p): pass\n",
    "elif": "if False:\n    pass\nelif True:\n    def guard(p): pass\n",
    "else": "if False:\n    pass\nelse:\n    def guard(p): pass\n",
    "try": "try:\n    def guard(p): pass\nexcept Exception:\n    pass\n",
    "except": "try:\n    pass\nexcept Exception:\n    def guard(p): pass\n",
    "try-else": (
        "try:\n    pass\nexcept Exception:\n    pass\nelse:\n    def guard(p): pass\n"
    ),
    "finally": "try:\n    pass\nfinally:\n    def guard(p): pass\n",
    "try-star": "try:\n    pass\nexcept* Exception:\n    def guard(p): pass\n",
    "with": "with open('x') as f:\n    def guard(p): pass\n",
    # `async with`/`async for` вне корутины не компилируются — область
    # модуля и класса их не несёт; условный `async def` — несёт.
    "async-def": "if True:\n    async def guard(p): pass\n",
    "for": "for i in []:\n    def guard(p): pass\n",
    "for-else": "for i in []:\n    pass\nelse:\n    def guard(p): pass\n",
    "while": "while False:\n    def guard(p): pass\n",
    "while-else": "while False:\n    pass\nelse:\n    def guard(p): pass\n",
    "match": "match 1:\n    case 1:\n        def guard(p): pass\n",
    "nested-if": "if True:\n    if True:\n        def guard(p): pass\n",
}


@pytest.mark.parametrize(
    "tail", CONDITIONAL_REDEFINITIONS.values(), ids=CONDITIONAL_REDEFINITIONS
)
def test_conditional_redefinition_is_input_error(tail: str) -> None:
    index = _index({MODULE_PATH: PLAIN_GUARD + tail})
    with pytest.raises(WiringInputError, match="guard"):
        enumerate_violations(index, _rule(function="guard", members=GUARD_ONLY))


ONLY_CONDITIONAL = """\
if True:
    def guard(previous, current) -> None:
        _guard_sessions(previous.spec_sessions, current.spec_sessions)
"""


def test_single_conditional_definition_is_input_error() -> None:
    index = _index({MODULE_PATH: ONLY_CONDITIONAL})
    with pytest.raises(WiringInputError, match="guard"):
        enumerate_violations(index, _rule(function="guard", members=GUARD_ONLY))


REBINDINGS = {
    "assign": "guard = print\n",
    "tuple-assign": "guard, other = print, print\n",
    "annassign": "guard: object = print\n",
    "augassign": "guard += 1\n",
    "from-import": "from os import guard\n",
    "from-import-as": "from os import path as guard\n",
    "import-as": "import os as guard\n",
    "import": "import guard\n",
    "del": "del guard\n",
    "for-target": "for guard in []:\n    pass\n",
    "with-target": "with open('x') as guard:\n    pass\n",
    "walrus": "(guard := print)\n",
    "except-name": "try:\n    pass\nexcept Exception as guard:\n    pass\n",
    "match-capture": "match 1:\n    case guard:\n        pass\n",
    "class": "class guard:\n    pass\n",
    "conditional-assign": "if True:\n    guard = print\n",
    "comprehension-walrus": "[(guard := x) for x in ()]\n",
    "decorator-walrus": "@(guard := staticmethod)\ndef other(): pass\n",
    # Аннотации, базы, ключевые аргументы и декораторы класса вычисляются в
    # объемлющей области: `:=` в них перепривязывает имя там же. Отложенные
    # аннотации (`from __future__ import annotations`) не делают исключения —
    # гейт консервативен и считает их такими же.
    "arg-annotation-walrus": "def other(p: (guard := print)): pass\n",
    "return-annotation-walrus": "def other() -> (guard := print): pass\n",
    "kwonly-annotation-walrus": "def other(*, p: (guard := print)): pass\n",
    "vararg-annotation-walrus": "def other(*a: (guard := print)): pass\n",
    "lambda-in-annotation-walrus": ("def g(p: (guard := lambda p: None)): pass\n"),
    "async-annotation-walrus": "async def other(p: (guard := print)): pass\n",
    "class-base-walrus": "class Other((guard := object)):\n    pass\n",
    "class-keyword-walrus": ("class Other(metaclass=(guard := type)):\n    pass\n"),
    "class-decorator-walrus": ("@(guard := staticmethod)\nclass Other:\n    pass\n"),
}


@pytest.mark.parametrize("tail", REBINDINGS.values(), ids=REBINDINGS)
def test_rebinding_in_same_scope_is_input_error(tail: str) -> None:
    index = _index({MODULE_PATH: PLAIN_GUARD + tail})
    with pytest.raises(WiringInputError, match="guard"):
        enumerate_violations(index, _rule(function="guard", members=GUARD_ONLY))


FUTURE_ANNOTATIONS_WALRUS = (
    "from __future__ import annotations\n\n"
    + PLAIN_GUARD
    + "def other(p: (guard := print)): pass\n"
)


def test_annotation_walrus_under_future_annotations_is_input_error() -> None:
    """Отложенные аннотации не освобождают от проверки: гейт консервативен."""
    index = _index({MODULE_PATH: FUTURE_ANNOTATIONS_WALRUS})
    with pytest.raises(WiringInputError, match="guard"):
        enumerate_violations(index, _rule(function="guard", members=GUARD_ONLY))


NESTED_SCOPES_ONLY = """\
def guard(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions)


def other():
    def guard(p):
        pass

    guard = print


class Holder:
    def guard(self, p):
        pass

    guard = print


handler = lambda guard: guard
names = [guard for guard in ()]
"""


def test_same_name_in_nested_scopes_is_not_ambiguous() -> None:
    index = _index({MODULE_PATH: NESTED_SCOPES_ONLY})
    rule = _rule(function="guard", members=GUARD_ONLY)
    assert _missing(index, rule) == []


PLAIN_METHOD = """\
class Guard:
    def check(self, previous, current) -> None:
        _guard_sessions(previous.spec_sessions, current.spec_sessions)
"""

METHOD_CASES = {
    "conditional-method": PLAIN_METHOD
    + "    if True:\n        def check(self, p): pass\n",
    "method-assign": PLAIN_METHOD + "    check = print\n",
    "method-import": PLAIN_METHOD + "    from os import path as check\n",
    "only-conditional-method": (
        "class Guard:\n    if True:\n"
        "        def check(self, previous, current) -> None:\n"
        "            _guard_sessions(previous.spec_sessions, "
        "current.spec_sessions)\n"
    ),
    "conditional-class": PLAIN_METHOD
    + "if True:\n    class Guard:\n        def check(self, p): pass\n",
    "only-conditional-class": "if True:\n"
    + "".join(f"    {line}\n" for line in PLAIN_METHOD.splitlines()),
    "class-assign": PLAIN_METHOD + "Guard = object\n",
    "class-import": PLAIN_METHOD + "from os import path as Guard\n",
}


@pytest.mark.parametrize("source", METHOD_CASES.values(), ids=METHOD_CASES)
def test_class_method_conditional_or_rebound_is_input_error(source: str) -> None:
    index = _index({MODULE_PATH: source})
    with pytest.raises(WiringInputError, match="Guard"):
        enumerate_violations(index, _rule(function="Guard.check", members=GUARD_ONLY))


def test_plain_class_method_still_analysed() -> None:
    index = _index({MODULE_PATH: PLAIN_METHOD})
    rule = _rule(function="Guard.check", members=GUARD_ONLY)
    assert _missing(index, rule) == []


# --- декоратор подменяет функцию — Unverifiable -------------------------------

# Python связывает с именем результат декоратора, а не разобранное тело:
# `@replace` может вернуть функцию, которая ничего не проверяет. Гейт не
# выносит суждения о декорированной функции (§3.5).

DECORATED_FUNCTION = """\
def replace(f):
    return lambda previous, current: None


@replace
def guard(previous, current) -> None:
    _guard_sessions(previous.spec_sessions, current.spec_sessions)
"""

DECORATED_METHOD = """\
class Guard:
    @staticmethod
    def check(previous, current) -> None:
        _guard_sessions(previous.spec_sessions, current.spec_sessions)
"""


@pytest.mark.parametrize(
    ("source", "function", "line"),
    [
        pytest.param(DECORATED_FUNCTION, "guard", 6, id="function"),
        pytest.param(DECORATED_METHOD, "Guard.check", 3, id="method"),
    ],
)
def test_decorated_target_is_unverifiable(
    source: str, function: str, line: int
) -> None:
    index = _index({MODULE_PATH: source})
    result = enumerate_violations(index, _rule(function=function, members=GUARD_ONLY))
    assert isinstance(result, Unverifiable)
    assert result.site == f"{MODULE_PATH}:{line}"
    assert "декоратор" in result.reason


# --- объемлющий класс `Class.method`: декоратор, метакласс, перепривязка -----

# Декоратор класса или метакласс может подменить метод после разбора тела:
# с `Guard.check` связан уже не разобранный `def`. Гейт не выносит суждения —
# `Unverifiable` (§3.5). Присваивание/`del` атрибута `Guard.check` в области
# модуля — перепривязка, как и прочие, код 2 (§3.5).

CLASS_REPLACED_BY_DECORATOR = """\
def replace(cls):
    cls.check = lambda self, p: None
    return cls


@replace
class Guard:
    def check(self, p):
        _check(p.a)
"""

CLASS_WITH_METACLASS = """\
class M(type):
    pass


class Guard(metaclass=M):
    def check(self, p):
        _check(p.a)
"""

CLASS_WITH_KWARGS_UNPACK = """\
class Guard(**options):
    def check(self, p):
        _check(p.a)
"""

UNDECORATED_CLASS = """\
class Guard:
    def check(self, p):
        _check(p.a)
"""


def _class_rule(members: tuple[str, ...] = ("a",)) -> EnumerateRule:
    return _rule(function="Guard.check", checkers=("_check",), members=members)


@pytest.mark.parametrize(
    ("source", "line", "reason"),
    [
        pytest.param(CLASS_REPLACED_BY_DECORATOR, 8, "декоратор", id="decorator"),
        pytest.param(CLASS_WITH_METACLASS, 6, "метакласс", id="metaclass"),
        pytest.param(CLASS_WITH_KWARGS_UNPACK, 2, "метакласс", id="kwargs-unpack"),
    ],
)
def test_class_hook_on_enclosing_class_is_unverifiable(
    source: str, line: int, reason: str
) -> None:
    index = _index({MODULE_PATH: source})
    result = enumerate_violations(index, _class_rule())
    assert isinstance(result, Unverifiable)
    assert result.site == f"{MODULE_PATH}:{line}"
    assert reason in result.reason


def test_undecorated_class_still_yields_violation() -> None:
    """Позитивный контроль: обычный класс без перепривязки разбирается."""
    index = _index({MODULE_PATH: UNDECORATED_CLASS})
    assert _missing(index, _class_rule(members=("a", "b"))) == [
        Violation(
            rule="r",
            kind="enumerates-all",
            site=f"{MODULE_PATH}:2",
            member="b",
            count=1,
        )
    ]


ATTRIBUTE_REBINDINGS = {
    "assign": "Guard.check = print\n",
    "assign-under-if": "if True:\n    Guard.check = print\n",
    "del": "del Guard.check\n",
    "annotated": "Guard.check: object = print\n",
    "augmented": "Guard.check += print\n",
    "tuple-target": "Guard.check, other = print, print\n",
    "for-target": "for Guard.check in ():\n    pass\n",
    "with-target": "with ctx() as Guard.check:\n    pass\n",
}


@pytest.mark.parametrize(
    "tail", ATTRIBUTE_REBINDINGS.values(), ids=ATTRIBUTE_REBINDINGS
)
def test_attribute_rebinding_of_class_method_is_input_error(tail: str) -> None:
    index = _index({MODULE_PATH: UNDECORATED_CLASS + tail})
    with pytest.raises(WiringInputError, match=r"Guard\.check"):
        enumerate_violations(index, _class_rule())


def test_attribute_rebinding_in_nested_scope_is_not_detected() -> None:
    """Граница (§3.5): мутация из тела функции — динамика, гейт её не видит."""
    source = UNDECORATED_CLASS + "def patch():\n    Guard.check = print\n"
    index = _index({MODULE_PATH: source})
    assert _missing(index, _class_rule()) == []


def test_other_attribute_assignment_is_not_rebinding() -> None:
    source = UNDECORATED_CLASS + "Guard.other = print\nOther.check = print\n"
    index = _index({MODULE_PATH: source})
    assert _missing(index, _class_rule()) == []


def test_method_rebinding_inside_class_body_is_input_error() -> None:
    source = UNDECORATED_CLASS + "    check = print\n"
    index = _index({MODULE_PATH: source})
    with pytest.raises(WiringInputError, match="check"):
        enumerate_violations(index, _class_rule())


def test_cli_decorated_enclosing_class_exits_failed(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Сквозь CLI: подменённый декоратором класса метод — `unverifiable`, код 1."""
    module = git_repo / "src" / "m.py"
    module.parent.mkdir(parents=True)
    module.write_text(CLASS_REPLACED_BY_DECORATOR, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "src"], cwd=git_repo, check=True)
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD:src"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    rule_body = (
        '[[rule]]\nid = "r"\nkind = "enumerates-all"\nmodule = "src/m.py"\n'
        'function = "Guard.check"\ncheckers = ["_check"]\nmembers = ["a"]\n'
    )
    (git_repo / "spec.md").write_text(
        f"# Спека\n\n```disputatio-wiring\n{rule_body}```\n", encoding="utf-8"
    )
    (git_repo / "plan.md").write_text(
        f'# План\n\n```disputatio-wiring-cover\nsrc_tree = "{tree}"\n```\n',
        encoding="utf-8",
    )

    code = main(
        ["gate", "wiring", "--spec", "spec.md", "--plan", "plan.md"]
        + ["--root", str(git_repo)]
    )

    assert code == EXIT_FAILED
    out = capsys.readouterr().out
    assert "unverifiable" in out
    assert "src/m.py:8" in out
