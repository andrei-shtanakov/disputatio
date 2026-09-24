"""Разбор деклараций гейта wiring: правила спеки и покрытие плана (design §3, §5).

Спека несёт **ровно один** fenced-блок `disputatio-wiring` (TOML) с массивом
`rule`: каждая запись — `construct-only-in` (§3.2) либо `enumerates-all`
(§3.5). План несёт **ровно один** блок `disputatio-wiring-cover` (§5.1) с
отпечатком снимка (`src_tree`) и массивом `cover`, связывающим найденные
нарушения с задачами плана. Оба блока разбираются `tomllib.loads`, затем
явной сверкой множества ключей: схема закрыта, неизвестный ключ и
отсутствующее обязательное поле — `WiringInputError` (код 2, §6), как и
любое другое непригодное значение (битый `id`, пустой список, `class` не
вида `модуль:Имя`).

Согласованность `member` с видом объявленного правила (`enumerates-all`
несёт `member`, `construct-only-in` его не несёт) — междокументная
проверка: вид правила живёт в спеке, запись покрытия — в плане. Здесь
`member` только типизируется (строка или отсутствие); саму сверку делает
последующая задача гейта.

Markdown модуль сам не разбирает: fenced-блоки и видимый текст разделов
задач (§5.3) он получает через протокол `PlanReader`, реализация которого —
CommonMark-парсер вне `verifier` (`disputatio.plan_markdown`, INV-10).

Вторая часть модуля — индекс модулей снимка (`build_index`) и правило
`construct-only-in` (`construct_violations`, §3.2): статическое разрешение
имён по снимку (§3.4) через множества объектов, с отсечением циклов по
активному стеку обхода.

Третья часть — правило `enumerates-all` (`enumerate_violations`, §3.5):
проверка фактического перебора состава в узкой форме тела функции.
Неподдерживаемая форма — не нарушение и не код `2`, а находка
`Unverifiable`: гейт отказывается судить, а не молча засчитывает успех.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Protocol

from disputatio.verifier.wiring_snapshot import Snapshot, WiringInputError


@dataclass(frozen=True, slots=True)
class ConstructRule:
    """Правило `construct-only-in` (§3.2): класс конструируется только в `allowed`."""

    id: str
    class_module: str
    class_name: str
    allowed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EnumerateRule:
    """Правило `enumerates-all` (§3.5): функция обязана перебрать все `members`."""

    id: str
    module: str
    function: str
    checkers: tuple[str, ...]
    members: tuple[str, ...]


Rule = ConstructRule | EnumerateRule


@dataclass(frozen=True, slots=True)
class Cover:
    """Запись покрытия одного нарушения задачей плана (§5.1)."""

    rule: str
    site: str
    member: str | None
    task: int


@dataclass(frozen=True, slots=True)
class CoverBlock:
    """Блок покрытия плана целиком: отпечаток снимка и все записи (§5.1)."""

    src_tree: str
    covers: tuple[Cover, ...]


# `id` правила — `[a-z0-9][a-z0-9._-]{0,63}` (§3.1): строчные латинские буквы
# и цифры, разделители `.`/`_`/`-`, первый символ не разделитель.
# Проверяется `fullmatch`: `$` в `re.match` пропускает хвостовой `\n`.
_RULE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

_CONSTRUCT_ONLY_IN = "construct-only-in"
_ENUMERATES_ALL = "enumerates-all"

_CONSTRUCT_RULE_FIELDS = frozenset({"id", "kind", "class", "allowed"})
_ENUMERATE_RULE_FIELDS = frozenset(
    {"id", "kind", "module", "function", "checkers", "members"}
)


class PlanReader(Protocol):
    """Markdown-сторона гейта (§5.3): всё, что гейт читает из спеки и плана.

    `verifier` не импортирует сторонних пакетов (INV-10) и не разбирает
    Markdown сам: реализация — `disputatio.plan_markdown.MarkdownPlanReader`
    на CommonMark-парсере — передаётся явно, без значения по умолчанию.
    """

    def fenced_blocks(self, text: str, info: str) -> list[str]:
        """Содержимое настоящих fenced-блоков с info-строкой ровно `info`."""
        ...

    def task_sections(self, text: str) -> Mapping[int, str]:
        """Номер задачи → видимый текст её раздела; повтор номера — код 2."""
        ...


def parse_rules(spec_text: str, *, reader: PlanReader) -> tuple[Rule, ...]:
    """Правила из единственного блока `disputatio-wiring` спеки (§3.1).

    Блок обязан существовать в единственном числе и нести непустой массив
    `rule`: пустой или отсутствующий массив дал бы зелёный гейт без единой
    проверки, а обязательность блока тогда ничего бы не значила.
    """
    document = _load_block(spec_text, "disputatio-wiring", "спека", reader)
    _check_schema(document, required={"rule"}, context="блок `disputatio-wiring`")
    raw_rules = document["rule"]
    if not isinstance(raw_rules, list) or len(raw_rules) == 0:
        raise WiringInputError(
            "блок `disputatio-wiring` обязан нести непустой массив `rule` (§3.1)"
        )
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        rule = _parse_rule(raw_rule, index)
        if rule.id in seen_ids:
            raise WiringInputError(f"повторный `id` правила: {rule.id!r} (§3.1)")
        seen_ids.add(rule.id)
        rules.append(rule)
    return tuple(rules)


def parse_cover(plan_text: str, *, reader: PlanReader) -> CoverBlock:
    """Блок покрытия из единственного блока `disputatio-wiring-cover` плана (§5.1)."""
    document = _load_block(plan_text, "disputatio-wiring-cover", "план", reader)
    _check_schema(
        document,
        required={"src_tree"},
        optional={"cover"},
        context="блок `disputatio-wiring-cover`",
    )
    src_tree = document["src_tree"]
    if not isinstance(src_tree, str) or not src_tree:
        raise WiringInputError(
            "блок `disputatio-wiring-cover`: `src_tree` обязан быть непустой "
            "строкой (§5.1)"
        )
    raw_covers = document.get("cover", [])
    if not isinstance(raw_covers, list):
        raise WiringInputError(
            "блок `disputatio-wiring-cover`: `cover` обязан быть массивом (§5.1)"
        )
    covers = tuple(
        _parse_cover_entry(raw_cover, index)
        for index, raw_cover in enumerate(raw_covers)
    )
    return CoverBlock(src_tree=src_tree, covers=covers)


def _load_block(
    text: str, info: str, document_name: str, reader: PlanReader
) -> dict[str, object]:
    """Единственный блок `info` документа `text`, разобранный как TOML."""
    blocks = reader.fenced_blocks(text, info)
    if len(blocks) == 0:
        raise WiringInputError(
            f"блок `{info}` не найден в {document_name} (ровно один обязателен)"
        )
    if len(blocks) > 1:
        raise WiringInputError(
            f"в {document_name} обязан быть ровно один блок `{info}`, "
            f"найдено {len(blocks)}"
        )
    try:
        return tomllib.loads(blocks[0])
    except tomllib.TOMLDecodeError as exc:
        raise WiringInputError(f"блок `{info}` не разбирается как TOML: {exc}") from exc


# --- схема -------------------------------------------------------------------


def _check_schema(
    mapping: Mapping[str, object],
    *,
    required: AbstractSet[str],
    optional: AbstractSet[str] = frozenset(),
    context: str,
) -> None:
    """Закрытая схема: только `required | optional`, все `required` присутствуют."""
    keys = set(mapping)
    allowed = required | optional
    unknown = keys - allowed
    if unknown:
        raise WiringInputError(
            f"{context}: неизвестный ключ {sorted(unknown)} "
            f"(допустимы {sorted(allowed)})"
        )
    missing = required - keys
    if missing:
        raise WiringInputError(
            f"{context}: не хватает обязательного поля {sorted(missing)}"
        )


def _require_id(value: object, context: str) -> str:
    if not isinstance(value, str) or not _RULE_ID_RE.fullmatch(value):
        raise WiringInputError(
            f"{context}: `id` обязан соответствовать `[a-z0-9][a-z0-9._-]{{0,63}}`: "
            f"{value!r} (§3.1)"
        )
    return value


def _require_nonempty_str(value: object, field: str, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise WiringInputError(f"{context}: `{field}` обязан быть непустой строкой")
    return value


def _require_nonempty_str_list(
    value: object, field: str, context: str
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) == 0:
        raise WiringInputError(
            f"{context}: `{field}` обязан быть непустым списком строк"
        )
    if not all(isinstance(item, str) for item in value):
        raise WiringInputError(f"{context}: `{field}` обязан содержать только строки")
    repeated = sorted({item for item in value if value.count(item) > 1})
    if repeated:
        raise WiringInputError(
            f"{context}: повтор элемента в `{field}`: {repeated} (§3.1)"
        )
    return tuple(value)


def _parse_class_field(value: str, context: str) -> tuple[str, str]:
    """`модуль:Имя` (§3.2): ровно один `:`, qualname без точки.

    v1 поддерживает только классы верхнего уровня.
    """
    if value.count(":") != 1:
        raise WiringInputError(
            f"{context}: `class` обязан быть вида `модуль:Имя`, ровно один "
            f"`:`: {value!r} (§3.2)"
        )
    module, _, qualname = value.partition(":")
    if not module or not qualname:
        raise WiringInputError(
            f"{context}: `class` обязан быть вида `модуль:Имя`: {value!r} (§3.2)"
        )
    if "." in qualname:
        raise WiringInputError(
            f"{context}: qualname класса не должен содержать точку (вложенные "
            f"классы не поддерживаются, v1): {value!r} (§3.2)"
        )
    return module, qualname


def _parse_rule(raw_rule: object, index: int) -> Rule:
    if not isinstance(raw_rule, dict):
        raise WiringInputError(f"`rule[{index}]` обязана быть таблицей TOML (§3.1)")
    context = f"`rule[{index}]`"
    kind = raw_rule.get("kind")
    if kind == _CONSTRUCT_ONLY_IN:
        return _parse_construct_rule(raw_rule, context)
    if kind == _ENUMERATES_ALL:
        return _parse_enumerate_rule(raw_rule, context)
    if "kind" not in raw_rule:
        raise WiringInputError(f"{context}: не хватает обязательного поля 'kind'")
    raise WiringInputError(f"{context}: неизвестный `kind` {kind!r} (§3.1)")


def _parse_construct_rule(raw: Mapping[str, object], context: str) -> ConstructRule:
    _check_schema(
        raw, required=_CONSTRUCT_RULE_FIELDS, context=f"{context} construct-only-in"
    )
    rule_id = _require_id(raw["id"], context)
    class_field = _require_nonempty_str(raw["class"], "class", context)
    class_module, class_name = _parse_class_field(class_field, context)
    allowed = _require_nonempty_str_list(raw["allowed"], "allowed", context)
    return ConstructRule(
        id=rule_id,
        class_module=class_module,
        class_name=class_name,
        allowed=allowed,
    )


def _parse_enumerate_rule(raw: Mapping[str, object], context: str) -> EnumerateRule:
    _check_schema(
        raw, required=_ENUMERATE_RULE_FIELDS, context=f"{context} enumerates-all"
    )
    rule_id = _require_id(raw["id"], context)
    module = _require_nonempty_str(raw["module"], "module", context)
    function = _require_nonempty_str(raw["function"], "function", context)
    checkers = _require_nonempty_str_list(raw["checkers"], "checkers", context)
    members = _require_nonempty_str_list(raw["members"], "members", context)
    return EnumerateRule(
        id=rule_id,
        module=module,
        function=function,
        checkers=checkers,
        members=members,
    )


_COVER_ENTRY_REQUIRED = frozenset({"rule", "site", "task"})
_COVER_ENTRY_OPTIONAL = frozenset({"member"})


def _parse_cover_entry(raw: object, index: int) -> Cover:
    if not isinstance(raw, dict):
        raise WiringInputError(f"`cover[{index}]` обязана быть таблицей TOML (§5.1)")
    context = f"`cover[{index}]`"
    _check_schema(
        raw,
        required=_COVER_ENTRY_REQUIRED,
        optional=_COVER_ENTRY_OPTIONAL,
        context=context,
    )
    rule = _require_nonempty_str(raw["rule"], "rule", context)
    site = _require_nonempty_str(raw["site"], "site", context)
    member = raw.get("member")
    if member is not None and not isinstance(member, str):
        raise WiringInputError(
            f"{context}: `member` обязан быть строкой или отсутствовать (§5.1)"
        )
    task = raw["task"]
    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        raise WiringInputError(
            f"{context}: `task` обязан быть целым числом ≥ 1: {task!r} (§5.1)"
        )
    return Cover(rule=rule, site=site, member=member, task=task)


# --- Индекс модулей и `construct-only-in` (§3.2, §3.4) -------------------------


@dataclass(frozen=True, slots=True)
class Violation:
    """Нарушение правила: место `путь:строка`, член и число вызовов (§3.2, §3.5)."""

    rule: str
    kind: str
    site: str
    member: str | None
    count: int


@dataclass(frozen=True, slots=True)
class Unverifiable:
    """Отказ судить правило `enumerates-all`: форма тела не поддержана (§3.5)."""

    rule: str
    site: str
    reason: str


@dataclass(frozen=True, slots=True)
class _ClassRef:
    """Объект разрешения «класс снимка» `module:name`."""

    module: str
    name: str


@dataclass(frozen=True, slots=True)
class _ModuleRef:
    """Объект разрешения «модуль» (источник привязки — и модуль вне снимка)."""

    name: str


@dataclass(frozen=True, slots=True)
class _External:
    """Объект разрешения «внешний»: всё, чего нет в снимке."""


@dataclass(frozen=True, slots=True)
class _MemberRef:
    """Источник привязки «член `(module, name)`» — `from module import name`."""

    module: str
    name: str


_Resolved = _ClassRef | _ModuleRef | _External
_Source = _ClassRef | _ModuleRef | _MemberRef

_EXTERNAL = _External()


@dataclass(frozen=True, slots=True)
class ModuleEntry:
    """Модуль снимка: путь, AST и привязки «имя → источники» (§3.4).

    Namespace-пакет — путь каталога, `tree = None` и пустые привязки.
    """

    path: str
    tree: ast.Module | None
    bindings: Mapping[str, tuple[_Source, ...]]


@dataclass(frozen=True, slots=True)
class ModuleIndex:
    """Индекс снимка под `--src`: модульное имя → запись; строится один раз."""

    modules: Mapping[str, ModuleEntry]
    files: AbstractSet[str]


def build_index(snapshot: Snapshot, src: str) -> ModuleIndex:
    """Индекс модулей снимка под `src` (§3.4, §4 «Разбор исходников»).

    Разбираются все `.py`-файлы под `src`; файлы вне него (например,
    `tests/`) не анализируются. Файл, который не декодируется или не
    разбирается, звёздочный импорт модуля снимка и относительный импорт
    за корень `src` — `WiringInputError` с именем файла.
    """
    prefix = src.rstrip("/") + "/"
    paths = sorted(path for path in snapshot.files if path.startswith(prefix))
    trees = {path: _parse_source(path, snapshot.files[path]) for path in paths}
    names = {path: _module_name(path[len(prefix) :]) for path in paths}
    _check_name_collisions(names)
    packages = {path for path in paths if path.endswith("/__init__.py")}
    known = set(names.values()) | _namespace_names(names.values())
    modules: dict[str, ModuleEntry] = {}
    for name in sorted(known - set(names.values())):
        modules[name] = ModuleEntry(
            path=prefix + name.replace(".", "/"), tree=None, bindings={}
        )
    for path in paths:
        name = names[path]
        package = name if path in packages else name.rpartition(".")[0]
        bindings = _collect_bindings(trees[path], name, package, path, known)
        modules[name] = ModuleEntry(path=path, tree=trees[path], bindings=bindings)
    return ModuleIndex(modules=modules, files=frozenset(paths))


def construct_violations(index: ModuleIndex, rule: ConstructRule) -> list[Violation]:
    """Нарушения `construct-only-in`: строки с вызовами класса вне `allowed` (§3.2).

    Сначала проверяется пригодность правила снимку: модуль класса есть,
    в нём есть класс верхнего уровня с этим именем, каждый путь `allowed`
    есть в снимке — иначе `WiringInputError`. Несколько вызовов на одной
    строке дают одно нарушение с `count`.
    """
    _check_construct_rule(index, rule)
    target = _ClassRef(rule.class_module, rule.class_name)
    allowed = set(rule.allowed)
    violations: list[Violation] = []
    for name, entry in sorted(index.modules.items(), key=lambda kv: kv[1].path):
        if entry.tree is None or entry.path in allowed:
            continue
        counts: dict[int, int] = {}
        for node in ast.walk(entry.tree):
            if isinstance(node, ast.Call) and target in _resolve_func(
                index, name, node.func
            ):
                counts[node.lineno] = counts.get(node.lineno, 0) + 1
        violations.extend(
            Violation(
                rule=rule.id,
                kind=_CONSTRUCT_ONLY_IN,
                site=f"{entry.path}:{line}",
                member=None,
                count=count,
            )
            for line, count in sorted(counts.items())
        )
    return violations


def _check_construct_rule(index: ModuleIndex, rule: ConstructRule) -> None:
    """Предусловия `construct-only-in` (§3.2); нарушение — `WiringInputError`."""
    entry = index.modules.get(rule.class_module)
    if entry is None:
        raise WiringInputError(
            f"правило {rule.id!r}: модуля класса `{rule.class_module}` "
            "нет в снимке (§3.2)"
        )
    top_classes = (
        set()
        if entry.tree is None
        else {n.name for n in entry.tree.body if isinstance(n, ast.ClassDef)}
    )
    if rule.class_name not in top_classes:
        raise WiringInputError(
            f"правило {rule.id!r}: в `{entry.path}` нет класса верхнего уровня "
            f"`{rule.class_name}` (§3.2)"
        )
    for path in rule.allowed:
        if path not in index.files:
            raise WiringInputError(
                f"правило {rule.id!r}: путь `allowed` {path!r} "
                "отсутствует в снимке (§3.2)"
            )


# --- `enumerates-all` (§3.5) ---------------------------------------------------

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


class _UnsupportedBody(Exception):
    """Внутренний сигнал неподдерживаемой формы тела функции (§3.5).

    Не покидает модуль: `enumerate_violations` ловит его и превращает в
    `Unverifiable` вместе с координатой места.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def enumerate_violations(
    index: ModuleIndex, rule: EnumerateRule
) -> list[Violation] | Unverifiable:
    """Нарушения `enumerates-all`: члены `members`, не перебранные функцией (§3.5).

    Сначала проверяется пригодность правила снимку: модуль есть в снимке как
    разобранный файл, функция с qualname `rule.function` (`f` либо
    `Class.method`) в нём существует — иначе `WiringInputError` (код 2).
    Дальше тело функции разбирается по узкой форме §3.5; любое отклонение от
    неё — не код 2 и не нарушение, а `Unverifiable`: перебор не подтверждён
    и не опровергнут, гейт не выносит суждения.
    """
    path, tree = _find_enumerate_module(index, rule)
    function, owner = _find_function(tree, rule)
    site = f"{path}:{function.lineno}"
    try:
        if owner is not None:
            _check_owner_class(owner)
        checked = _checked_members(function, rule)
    except _UnsupportedBody as exc:
        return Unverifiable(rule=rule.id, site=site, reason=exc.reason)
    missing = [member for member in rule.members if member not in checked]
    return [
        Violation(rule=rule.id, kind=_ENUMERATES_ALL, site=site, member=member, count=1)
        for member in missing
    ]


def _find_enumerate_module(
    index: ModuleIndex, rule: EnumerateRule
) -> tuple[str, ast.Module]:
    """Путь и AST модуля правила по `rule.module` (путь от корня репозитория).

    Не найден, либо найден как namespace-пакет (без собственного AST), —
    `WiringInputError` (§3.5): в обоих случаях функции в нём быть не может.
    """
    for entry in index.modules.values():
        if entry.path == rule.module and entry.tree is not None:
            return entry.path, entry.tree
    raise WiringInputError(
        f"правило {rule.id!r}: модуля `{rule.module}` нет в снимке (§3.5)"
    )


def _find_function(
    tree: ast.Module, rule: EnumerateRule
) -> tuple[_FunctionNode, ast.ClassDef | None]:
    """Функция по qualname `rule.function` и её класс (`None` для `f`).

    `f` — верхнего уровня, `Class.method` — метод класса верхнего уровня.
    Другая глубина qualname (несколько точек) правилом не поддерживается и
    даёт тот же отказ, что и отсутствующая функция (§3.5: только эти две
    формы описаны). Искомое имя обязано быть привязано в своей области
    видимости ровно одним безусловным определением (`_single_definition`);
    для `Class.method` атрибут `Class.method` к тому же не перепривязан в
    области модуля (`_check_attribute_rebinding`), а `check` не объявлен
    `global`/`nonlocal` в теле класса (`_check_class_scope_declaration`) —
    такое объявление перенаправляет `def check` в объемлющую область, и
    метод в классе не связывается; иначе — `WiringInputError` (§3.5).
    """
    parts = rule.function.split(".")
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    if len(parts) == 1:
        return _single_definition(tree.body, parts[0], kinds, rule), None
    if len(parts) != 2:
        raise WiringInputError(
            f"правило {rule.id!r}: `function` {rule.function!r} не "
            "поддерживается — только `f` или `Class.method` (§3.5)"
        )
    class_node = _single_definition(tree.body, parts[0], ast.ClassDef, rule)
    _check_class_scope_declaration(class_node.body, parts[1], rule)
    function = _single_definition(class_node.body, parts[1], kinds, rule)
    _check_attribute_rebinding(tree.body, parts[0], parts[1], rule)
    return function, class_node


def _check_class_scope_declaration(
    body: list[ast.stmt], method: str, rule: EnumerateRule
) -> None:
    """Отказ, если `global`/`nonlocal method` перенаправляет `def method`.

    `global check` (или `nonlocal check`) в теле класса — это не находка и
    не привязка сама по себе, но следующий `def check` в той же области
    вяжет имя не в пространство имён класса, а в объемлющую область: атрибут
    `Class.method` тогда не существует, хотя `_single_definition` находит
    единственный безусловный `def` и без этой проверки прошла бы молча
    (§3.5). Обходится вся область тела класса, включая вложенные составные
    операторы (`_scope_nodes`), но не тела вложенных `def`/`class`/`lambda`.
    Правило не применяется к телу модуля: `global f` там — не операция
    (модульная область и так глобальная).
    """
    for node in _scope_nodes(body):
        if isinstance(node, (ast.Global, ast.Nonlocal)) and method in node.names:
            keyword = "global" if isinstance(node, ast.Global) else "nonlocal"
            raise WiringInputError(
                f"правило {rule.id!r}: `{keyword} {method}` в теле класса "
                f"(строка {node.lineno}) — `{method}` не привязывается в "
                "пространстве имён класса, `def` уходит в объемлющую "
                "область (§3.5)"
            )


def _check_attribute_rebinding(
    body: list[ast.stmt], class_name: str, method: str, rule: EnumerateRule
) -> None:
    """Отказ, если атрибут `class_name.method` перепривязан в области `body`.

    Цель присваивания (в том числе аннотированного и составного), `del`,
    цель `for`/`with` вида `Class.method` в области модуля (обход — тот же,
    что у `_scope_bindings`) подменяет разобранный метод — код 2 (§3.5).
    Цель comprehension'а/generator-выражения — тоже: простое имя цели
    comprehension-локально и не обходится (см. `_same_scope_children`), но
    её элемент-атрибут — в том числе внутри распаковки `Tuple`/`List`/
    `Starred` и при нескольких `for` — обходится как обычная цель. Мутация
    из тел функций, `setattr` и чужих модулей — объявленная граница, не
    отслеживается.
    """
    for node in _scope_nodes(body):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.attr == method
            and isinstance(node.value, ast.Name)
            and node.value.id == class_name
        ):
            raise WiringInputError(
                f"правило {rule.id!r}: атрибут `{class_name}.{method}` "
                f"перепривязан в `{rule.module}` (строка {node.lineno}) — "
                "разобранное тело метода не то, что связано с классом (§3.5)"
            )


def _check_owner_class(class_node: ast.ClassDef) -> None:
    """`_UnsupportedBody`, если класс может подменить свой метод (§3.5).

    Декоратор класса и метакласс (`metaclass=` или распаковка `**` в
    операторе `class`, которая может его нести) исполняются после тела
    класса и вправе заменить метод: с `Class.method` связано уже не
    разобранное тело.
    """
    if class_node.decorator_list:
        raise _UnsupportedBody(
            "класс под декоратором: декоратор класса может подменить метод, "
            "с `Class.method` связано не разобранное тело (§3.5)"
        )
    if any(keyword.arg in (None, "metaclass") for keyword in class_node.keywords):
        raise _UnsupportedBody(
            "класс с метаклассом (`metaclass=` или `**` в операторе `class`): "
            "метакласс может подменить метод (§3.5)"
        )


def _single_definition[T: ast.stmt](
    body: list[ast.stmt],
    name: str,
    kind: type[T] | tuple[type[T], ...],
    rule: EnumerateRule,
) -> T:
    """Единственное безусловное определение `name` вида `kind` в области `body`.

    Привязки имени собираются со всей области (`_scope_bindings`): прямо из
    `body` и из вложенных составных операторов, но не из тел вложенных
    `def`/`class`/`lambda`. Нет ни одной — отказ «не найдено». Больше
    одной (второе определение, присваивание, импорт, `del`, …) либо
    единственная, но вида не `kind` или под составным оператором, —
    отказ «неоднозначно»: Python связывает то, что выполнилось последним,
    а условное определение гейт статически не разрешает; молчаливый разбор
    одного из узлов читал бы не тот объект (§3.5).
    """
    bindings = _scope_bindings(body, name)
    what = "класса" if kind is ast.ClassDef else "функции"
    if not bindings:
        raise WiringInputError(
            f"правило {rule.id!r}: {what} `{name}` нет в области видимости "
            f"`{rule.function}` в `{rule.module}` (§3.5)"
        )
    node = bindings[0]
    if (
        len(bindings) > 1
        or not isinstance(node, kind)
        or not any(node is statement for statement in body)
    ):
        raise WiringInputError(
            f"правило {rule.id!r}: имя `{name}` (`{rule.function}`) в "
            f"`{rule.module}` определено неоднозначно — {len(bindings)} "
            "привязок в области видимости, условное определение или "
            "перепривязка (§3.5)"
        )
    return node


def _scope_bindings(body: list[ast.stmt], name: str) -> list[ast.AST]:
    """Узлы, привязывающие или удаляющие `name` в области `body` (§3.5).

    Обходится вся область: операторы `body` и вложенные составные операторы
    (`if`, `try`, `with`, циклы, `match`) на любой глубине. Тела вложенных
    `def`/`async def`/`class`/`lambda` — другая область и не обходятся;
    их декораторы, значения по умолчанию, аннотации, базы и имя самой
    вложенной функции/класса вычисляются и привязываются в этой области и
    обходятся. Порядок узлов — порядок исходника.
    """
    found = [node for node in _scope_nodes(body) if _binds(node, name)]
    return sorted(found, key=lambda node: (node.lineno, node.col_offset))


def _scope_nodes(body: list[ast.stmt]) -> Iterable[ast.AST]:
    """Все узлы области видимости `body` без тел вложенных областей."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(_same_scope_children(node))


def _same_scope_children(node: ast.AST) -> list[ast.AST]:
    """Дочерние узлы `node`, вычисляемые в той же области видимости."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        kw_defaults = [value for value in args.kw_defaults if value is not None]
        defaults: list[ast.AST] = [*args.defaults, *kw_defaults]
        if isinstance(node, ast.Lambda):
            return defaults
        # Аннотации параметров и возврата вычисляются в объемлющей области.
        # Отложенные (`from __future__ import annotations`) тоже обходятся:
        # гейт консервативен и не различает режим аннотаций.
        params = [
            *args.posonlyargs,
            *args.args,
            *([args.vararg] if args.vararg else []),
            *args.kwonlyargs,
            *([args.kwarg] if args.kwarg else []),
        ]
        annotations = [param.annotation for param in params if param.annotation]
        returns = [node.returns] if node.returns else []
        return [*node.decorator_list, *defaults, *annotations, *returns]
    if isinstance(node, ast.ClassDef):
        keywords = [keyword.value for keyword in node.keywords]
        return [*node.decorator_list, *node.bases, *keywords]
    if isinstance(node, ast.comprehension):
        # Переменная comprehension живёт в его собственной области; `:=` в
        # условиях и элементе привязывает во внешней и обходится. Плоское
        # имя цели (в том числе внутри `Tuple`/`List`/`Starred`) — тоже
        # comprehension-локально и не обходится; но цель-атрибут/подписка
        # (`Guard.check`) мутирует объект внешней области — реальная
        # семантика Python, не comprehension-локальная переменная — и
        # обходится как обычная цель присваивания (§3.5).
        return [node.iter, *node.ifs, *_non_name_target_parts(node.target)]
    return list(ast.iter_child_nodes(node))


def _non_name_target_parts(target: ast.expr) -> list[ast.expr]:
    """Части цели `target`, не являющиеся простым именем, рекурсивно.

    `Attribute`/`Subscript` возвращаются целиком — перепривязка атрибута
    класса ищется в них обычным обходом их дочерних узлов. `Starred`
    разворачивается в значение. `Tuple`/`List` — в элементы. Простое `Name`
    исключается: оно comprehension-локально.
    """
    if isinstance(target, (ast.Attribute, ast.Subscript)):
        return [target]
    if isinstance(target, ast.Starred):
        return _non_name_target_parts(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [
            part for element in target.elts for part in _non_name_target_parts(element)
        ]
    return []


def _binds(node: ast.AST, name: str) -> bool:
    """`node` привязывает или удаляет `name` в своей области видимости."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.Name):
        return node.id == name and isinstance(node.ctx, (ast.Store, ast.Del))
    if isinstance(node, ast.Import):
        return any(
            (alias.asname or alias.name.partition(".")[0]) == name
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        return any((alias.asname or alias.name) == name for alias in node.names)
    if isinstance(node, ast.ExceptHandler):
        return node.name == name
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return node.name == name
    if isinstance(node, ast.MatchMapping):
        return node.rest == name
    return False


def _checked_members(function: _FunctionNode, rule: EnumerateRule) -> set[str]:
    """Множество членов, фактически перебранных телом функции (§3.5).

    Необязательный докстринг первым оператором пропускается; остаток тела
    обязан состоять только из проверяющих вызовов. Первое отклонение от этой
    формы поднимает `_UnsupportedBody`. Декорированная функция — тоже: имя
    связано с результатом декоратора, а не с разобранным телом.
    """
    if function.decorator_list:
        raise _UnsupportedBody(
            "функция под декоратором: с именем связан результат декоратора, "
            "а не разобранное тело (§3.5)"
        )
    body = function.body
    if body and _is_docstring(body[0]):
        body = body[1:]
    params = _param_names(function)
    return {_checker_call_attr(statement, rule.checkers, params) for statement in body}


def _is_docstring(statement: ast.stmt) -> bool:
    """Докстринг — `ast.Expr` со строковой константой (§3.5, только 1-й оператор)."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _param_names(function: _FunctionNode) -> frozenset[str]:
    """Имена параметров: `args`, `posonlyargs`, `kwonlyargs`, `vararg`, `kwarg`."""
    args = function.args
    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return frozenset(names)


def _checker_call_attr(
    statement: ast.stmt, checkers: tuple[str, ...], params: AbstractSet[str]
) -> str:
    """`attr`, общий для всех атрибутных аргументов проверяющего вызова (§3.5).

    Не `ast.Expr(ast.Call)` с `func` из `checkers` — оператор неподдержан.
    Аргумент вида `параметр.attr` (параметр функции) засчитывается; прочие
    аргументы (строки, константы) игнорируются. Ни одного засчитанного
    аргумента, либо несовпадение `attr` между ними, — тоже неподдержанная
    форма: обе ветви поднимают `_UnsupportedBody`, а не молча выбирают одно
    значение.
    """
    if not isinstance(statement, ast.Expr):
        raise _UnsupportedBody("оператор тела не является проверяющим вызовом (§3.5)")
    call = statement.value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        raise _UnsupportedBody("оператор тела не является проверяющим вызовом (§3.5)")
    name = call.func.id
    if name not in checkers:
        raise _UnsupportedBody(f"вызов `{name}` не входит в `checkers` (§3.5)")
    attrs = {
        node.attr
        for node in (*call.args, *(kw.value for kw in call.keywords))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in params
    }
    if len(attrs) == 0:
        raise _UnsupportedBody(
            f"вызов `{name}` без атрибутного аргумента параметра (§3.5)"
        )
    if len(attrs) > 1:
        raise _UnsupportedBody(
            f"вызов `{name}` несёт разные `attr` в аргументах: {sorted(attrs)} (§3.5)"
        )
    return next(iter(attrs))


def _parse_source(path: str, data: bytes) -> ast.Module:
    """AST файла снимка; кодировку по PEP 263 определяет сам `ast.parse`."""
    try:
        return ast.parse(data, filename=path)
    except (SyntaxError, ValueError) as exc:
        raise WiringInputError(f"файл снимка не разбирается: {path}: {exc}") from exc


def _check_name_collisions(names: Mapping[str, str]) -> None:
    """Два файла с одним модульным именем — `WiringInputError` (§4).

    Индекс хранит одну запись на имя: без этой проверки второй файл молча
    выпал бы из анализа, а пропущенный файл мог бы скрыть конструктор.
    Namespace-пакет с тем же именем, что у файла-модуля, коллизией не
    считается: запись получает файл, как и в Python.
    """
    by_name: dict[str, list[str]] = {}
    for path, name in names.items():
        by_name.setdefault(name, []).append(path)
    for name, paths in sorted(by_name.items()):
        if len(paths) > 1:
            raise WiringInputError(
                f"коллизия модульного имени `{name}`: {', '.join(sorted(paths))}"
            )


def _module_name(relpath: str) -> str:
    """Модульное имя по пути от `--src`; `__init__.py` даёт имя пакета."""
    parts = relpath[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _namespace_names(module_names: Iterable[str]) -> set[str]:
    """Все пакеты-предки модулей: каталоги под `--src` на любой глубине."""
    prefixes: set[str] = set()
    for name in module_names:
        parts = name.split(".")
        prefixes.update(".".join(parts[:i]) for i in range(1, len(parts)))
    return prefixes


def _collect_bindings(
    tree: ast.Module,
    module: str,
    package: str,
    path: str,
    known: AbstractSet[str],
) -> dict[str, tuple[_Source, ...]]:
    """Привязки модуля со всего дерева, включая тела функций и классов (§3.4)."""
    bindings: dict[str, list[_Source]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bindings.setdefault(node.name, []).append(_ClassRef(module, node.name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is not None:
                    bound, source = alias.asname, alias.name
                else:
                    bound = source = alias.name.partition(".")[0]
                bindings.setdefault(bound, []).append(_ModuleRef(source))
        elif isinstance(node, ast.ImportFrom):
            origin = _import_origin(node, package, path)
            for alias in node.names:
                if alias.name == "*":
                    if origin in known:
                        raise WiringInputError(
                            f"{path}:{node.lineno}: `from {origin} import *` "
                            "из модуля снимка не поддерживается (§3.4)"
                        )
                    continue
                bound = alias.asname or alias.name
                bindings.setdefault(bound, []).append(_MemberRef(origin, alias.name))
    return {name: tuple(sources) for name, sources in bindings.items()}


def _import_origin(node: ast.ImportFrom, package: str, path: str) -> str:
    """Абсолютное имя модуля `from`-импорта; относительный — от пакета модуля."""
    if node.level == 0:
        return node.module or ""
    parts = package.split(".") if package else []
    if node.level > len(parts):
        raise WiringInputError(
            f"{path}:{node.lineno}: относительный импорт выходит за корень "
            "`--src` (§3.4)"
        )
    base = parts[: len(parts) - node.level + 1]
    if node.module:
        base.append(node.module)
    return ".".join(base)


def _resolve_func(
    index: ModuleIndex, module: str, func: ast.expr
) -> frozenset[_Resolved]:
    """Разрешение `func` вызова — самостоятельный запрос с новым стеком (§3.4)."""
    chain: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return frozenset()
    resolver = _Resolver(index)
    current = resolver.resolve(module, node.id)
    for attr in reversed(chain):
        current = resolver.attribute(current, attr)
    return current


_Key = tuple[str, str, str]


class _Resolver:
    """Обход `resolve`/`member` одного запроса; циклы — по активному стеку.

    Ключ обхода — `(операция, модуль, имя)`. Повторный вход в ключ, который
    лежит в активном стеке, даёт пустое множество; ключ снимается со
    стека при возврате, поэтому последовательный повтор ключа (звенья
    `pkg.self.self`) циклом не считается.
    """

    def __init__(self, index: ModuleIndex) -> None:
        self._index = index
        self._active: set[_Key] = set()

    def attribute(
        self, objects: frozenset[_Resolved], attr: str
    ) -> frozenset[_Resolved]:
        """Звено атрибута: `member(X, attr)` по модулям; прочее — внешний."""
        result: set[_Resolved] = set()
        for obj in objects:
            if isinstance(obj, _ModuleRef):
                result |= self.member(obj.name, attr)
            else:
                result.add(_EXTERNAL)
        return frozenset(result)

    def resolve(self, module: str, name: str) -> frozenset[_Resolved]:
        """`resolve(M, n)`: объединение по всем источникам имени `n` в `M`."""
        return self._guarded(("resolve", module, name), self._resolve)

    def member(self, module: str, name: str) -> frozenset[_Resolved]:
        """`member(X, N)`: что значит `X.N`; пусто или вне снимка — внешний."""
        return self._guarded(("member", module, name), self._member)

    def _guarded(
        self, key: _Key, step: Callable[[str, str], frozenset[_Resolved]]
    ) -> frozenset[_Resolved]:
        if key in self._active:
            return frozenset()
        self._active.add(key)
        try:
            return step(key[1], key[2])
        finally:
            self._active.discard(key)

    def _resolve(self, module: str, name: str) -> frozenset[_Resolved]:
        entry = self._index.modules.get(module)
        sources = () if entry is None else entry.bindings.get(name, ())
        result: set[_Resolved] = set()
        for source in sources:
            if isinstance(source, _MemberRef):
                result |= self.member(source.module, source.name)
            elif isinstance(source, _ModuleRef):
                known = source.name in self._index.modules
                result.add(source if known else _EXTERNAL)
            else:
                result.add(source)
        return frozenset(result)

    def _member(self, module: str, name: str) -> frozenset[_Resolved]:
        if module not in self._index.modules:
            return frozenset({_EXTERNAL})
        result = set(self.resolve(module, name))
        submodule = f"{module}.{name}"
        if submodule in self._index.modules:
            result.add(_ModuleRef(submodule))
        return frozenset(result or {_EXTERNAL})


# --- Отчёт гейта: покрытие и находки (§5.2, §5.3, §6) -------------------------

_UNCOVERED = "uncovered"
_DUPLICATE_COVER = "duplicate-cover"
_DANGLING_COVER = "dangling-cover"
_MISSING_TASK = "missing-task"
_SITE_NOT_IN_TASK = "site-not-in-task"
_UNVERIFIABLE = "unverifiable"
_STALE_SNAPSHOT = "stale-snapshot"
_DIRTY_SRC = "dirty-src"

# Ключ сопоставления нарушения и записи покрытия (§5.2): `(rule, site)` для
# `construct-only-in` и `(rule, site, member)` для `enumerates-all`; здесь
# всегда полная тройка, `member` — `None` для `construct-only-in`.
_CoverKey = tuple[str, str, str | None]


@dataclass(frozen=True, slots=True)
class Finding:
    """Находка гейта (§5.3, §6): проблема покрытия или непригодность снимка.

    `code` — один из восьми кодов §5.3/§6: `uncovered`, `duplicate-cover`,
    `dangling-cover`, `missing-task`, `site-not-in-task`, `unverifiable`
    (находки покрытия/пригодности), `stale-snapshot`, `dirty-src` (находки
    снимка). `rule`/`site`/`member` — координаты находки: те же, что у
    ключа покрытия (§5.2), пустая строка `rule`/`site` и `member is None`
    у `stale-snapshot`/`dirty-src`, у которых нет одной координаты.
    `detail` — свободное описание причины, печатаемое `render_report`.
    """

    code: str
    rule: str
    site: str
    member: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class WiringReport:
    """Итог гейта wiring: все нарушения обоих правил и все находки (§5.3, §6)."""

    violations: tuple[Violation, ...]
    findings: tuple[Finding, ...]


def check_wiring(
    *,
    spec_text: str,
    plan_text: str,
    snapshot: Snapshot,
    src: str,
    dirty: tuple[str, ...],
    reader: PlanReader,
) -> WiringReport:
    """Собирает отчёт гейта wiring в порядке «код `2` старше кода `1`» (§6).

    Порядок шагов обязателен:

    1. Разбор блока правил спеки, блока покрытия и разделов задач плана
       (`parse_rules`, `parse_cover`, `reader.task_sections`) — их собственные
       предусловия (§3.1, §5.1, §5.3) уже поднимают `WiringInputError`.
       Здесь же — междокументная сверка: `member` записи покрытия для
       объявленного `construct-only-in`, либо его отсутствие для
       объявленного `enumerates-all`, — тоже `WiringInputError`. Запись с
       необъявленным `rule` здесь не ошибка — это будущая находка
       `dangling-cover`.
    2. Индекс снимка (`build_index`) — тоже даёт `WiringInputError` на
       неразбираемом исходнике или непригодном импорте (§4, §3.4).
    3. Пригодность и нарушения **каждого** правила
       (`construct_violations`/`enumerate_violations`), даже если снимок
       ниже окажется грязным или устаревшим: код `2` предусловий правила
       (§3.2, §3.5) обязан подняться раньше находок `dirty-src`/
       `stale-snapshot`, а не быть ими заслонён.
    4. `dirty` непуст → единственная находка `dirty-src`; нарушения шага 3
       отбрасываются, покрытие не вычисляется.
    5. Отпечаток `cover_block.src_tree` не совпал со `snapshot.tree` →
       единственная находка `stale-snapshot`, тем же отбрасыванием.
    6. Иначе — находки покрытия (§5.2, §5.3) над нарушениями шага 3.
    """
    rules = parse_rules(spec_text, reader=reader)
    cover_block = parse_cover(plan_text, reader=reader)
    sections = reader.task_sections(plan_text)
    rules_by_id = {rule.id: rule for rule in rules}
    _check_cover_member_consistency(cover_block.covers, rules_by_id)

    index = build_index(snapshot, src)

    violations: list[Violation] = []
    unverifiables: list[Unverifiable] = []
    for rule in rules:
        if isinstance(rule, ConstructRule):
            violations.extend(construct_violations(index, rule))
        else:
            enumerate_result = enumerate_violations(index, rule)
            if isinstance(enumerate_result, Unverifiable):
                unverifiables.append(enumerate_result)
            else:
                violations.extend(enumerate_result)

    if dirty:
        detail = "src грязный: " + ", ".join(sorted(dirty))
        finding = Finding(code=_DIRTY_SRC, rule="", site="", member=None, detail=detail)
        return WiringReport(violations=(), findings=(finding,))

    if cover_block.src_tree != snapshot.tree:
        detail = (
            f"план собран для отпечатка {cover_block.src_tree}, "
            f"снимок несёт {snapshot.tree} (§4)"
        )
        finding = Finding(
            code=_STALE_SNAPSHOT, rule="", site="", member=None, detail=detail
        )
        return WiringReport(violations=(), findings=(finding,))

    findings = _coverage_findings(
        violations, unverifiables, cover_block.covers, rules_by_id, sections
    )
    return WiringReport(violations=tuple(violations), findings=tuple(findings))


def _check_cover_member_consistency(
    covers: tuple[Cover, ...], rules_by_id: Mapping[str, Rule]
) -> None:
    """`member` записи покрытия обязан соответствовать виду её правила (§5.1).

    Необъявленный `rule` здесь не проверяется — это будущая находка
    `dangling-cover` (§5.3.3), а не ошибка ввода.
    """
    for cover in covers:
        rule = rules_by_id.get(cover.rule)
        if rule is None:
            continue
        if isinstance(rule, ConstructRule) and cover.member is not None:
            raise WiringInputError(
                f"покрытие {cover.rule!r} на {cover.site!r}: `member` запрещён "
                "для construct-only-in (§5.1)"
            )
        if isinstance(rule, EnumerateRule) and cover.member is None:
            raise WiringInputError(
                f"покрытие {cover.rule!r} на {cover.site!r}: `member` обязателен "
                "для enumerates-all (§5.1)"
            )


def _coverage_findings(
    violations: Iterable[Violation],
    unverifiables: Iterable[Unverifiable],
    covers: tuple[Cover, ...],
    rules_by_id: Mapping[str, Rule],
    sections: Mapping[int, str],
) -> list[Finding]:
    """Находки покрытия (§5.3) над уже посчитанными нарушениями и `Unverifiable`."""
    violations_by_key: dict[_CoverKey, Violation] = {
        (v.rule, v.site, v.member): v for v in violations
    }
    covers_by_key: dict[_CoverKey, list[Cover]] = {}
    for cover in covers:
        key = (cover.rule, cover.site, cover.member)
        covers_by_key.setdefault(key, []).append(cover)

    findings: list[Finding] = [
        Finding(
            code=_UNCOVERED,
            rule=violation.rule,
            site=violation.site,
            member=violation.member,
            detail="",
        )
        for key, violation in violations_by_key.items()
        if key not in covers_by_key
    ]

    for key, entries in covers_by_key.items():
        rule_id, site, member = key
        if len(entries) > 1:
            findings.append(
                Finding(
                    code=_DUPLICATE_COVER,
                    rule=rule_id,
                    site=site,
                    member=member,
                    detail=f"{len(entries)} записей покрытия на один ключ (§5.2)",
                )
            )
        if key not in violations_by_key:
            reason = (
                "правило не объявлено (§5.3)"
                if rule_id not in rules_by_id
                else "нарушения по этому месту нет (§5.3)"
            )
            findings.append(
                Finding(
                    code=_DANGLING_COVER,
                    rule=rule_id,
                    site=site,
                    member=member,
                    detail=reason,
                )
            )

    for cover in covers:
        findings.extend(_task_reference_findings(cover, sections))

    findings.extend(
        Finding(
            code=_UNVERIFIABLE, rule=u.rule, site=u.site, member=None, detail=u.reason
        )
        for u in unverifiables
    )

    return sorted(findings, key=_finding_sort_key)


def _task_reference_findings(
    cover: Cover, sections: Mapping[int, str]
) -> list[Finding]:
    """`missing-task`/`site-not-in-task` для одной записи покрытия (§5.3.4-5)."""
    section = sections.get(cover.task)
    if section is None:
        return [
            Finding(
                code=_MISSING_TASK,
                rule=cover.rule,
                site=cover.site,
                member=cover.member,
                detail=f"задачи №{cover.task} нет в плане (§5.3)",
            )
        ]
    missing_site = not _mentions_site(section, cover.site)
    missing_member = cover.member is not None and not _mentions_member(
        section, cover.member
    )
    if not missing_site and not missing_member:
        return []
    if missing_site and missing_member:
        what = "`site` и `member`"
    elif missing_site:
        what = "`site`"
    else:
        what = "`member`"
    return [
        Finding(
            code=_SITE_NOT_IN_TASK,
            rule=cover.rule,
            site=cover.site,
            member=cover.member,
            detail=f"раздел задачи №{cover.task} не ссылается на {what} (§5.3)",
        )
    ]


def _mentions_site(section: str, site: str) -> bool:
    """`site` встречается в разделе целым токеном (§5.3), а не подстрокой.

    Слева — не продолжение пути (`[\\w./-]`), справа — не цифра: иначе
    `b.py:8` засчитывался бы упоминанием `b.py:80`, а `pkg/b.py:8` —
    упоминанием `old/pkg/b.py:8`. Пунктуация вокруг (кавычки, скобки,
    точка или запятая после, `:колонка`) ссылку не портит.
    """
    pattern = rf"(?<![\w./-]){re.escape(site)}(?!\d)"
    return re.search(pattern, section) is not None


def _mentions_member(section: str, member: str) -> bool:
    """`member` встречается в разделе целым словом (§5.3).

    Соседние символы — не `\\w`: `sessions` внутри `doc_sessions` или
    `sessions2` ссылкой на член не считается.
    """
    pattern = rf"(?<!\w){re.escape(member)}(?!\w)"
    return re.search(pattern, section) is not None


def _finding_sort_key(finding: Finding) -> tuple[str, str, str, str]:
    """Ключ сортировки находок — `(код, rule, site, member)` (§6); `None` — `""`."""
    return (finding.code, finding.rule, finding.site, finding.member or "")


def render_report(report: WiringReport) -> list[str]:
    """Строки вывода гейта (§6): по находке, последняя — сводка.

    Формат строки: `{code} [{kind}] [{rule}] [{site}] [member={member}]
    [count={count}] [{detail}]`. `{kind}`/`{count}` подставляются, только
    когда находка совпадает по ключу `(rule, site, member)` с элементом
    `report.violations`: тогда `kind` берётся из найденного `Violation`, а
    `count=` печатается лишь при `kind == "construct-only-in"` — ровно
    поведение, которого требует design §5.3 («count печатается у
    construct-only-in»). У находок `uncovered`/`duplicate-cover` ключ
    совпадает с нарушением почти всегда; у `dangling-cover` и
    `unverifiable` — по построению никогда, поэтому `kind`/`count` для них
    не печатаются. `rule`/`site` опускаются, если пусты (у
    `stale-snapshot`/`dirty-src` одной координаты нет); `member=` печатается,
    если он не `None`; `detail` — свободный текст находки, печатается
    последним, если не пуст.

    Находки отсортированы по `(code, rule, site, member)` (`_finding_sort_key`,
    `None`-`member` как пустая строка). Последняя строка — сводка
    `wiring: N нарушений, M покрыто, K находок`, где `M` — число нарушений
    без находки `uncovered`.
    """
    violations_by_key = {(v.rule, v.site, v.member): v for v in report.violations}
    findings = sorted(report.findings, key=_finding_sort_key)
    lines = [_render_finding(finding, violations_by_key) for finding in findings]
    uncovered = sum(1 for f in report.findings if f.code == _UNCOVERED)
    covered = len(report.violations) - uncovered
    lines.append(
        f"wiring: {len(report.violations)} нарушений, {covered} покрыто, "
        f"{len(report.findings)} находок"
    )
    return lines


def _render_finding(
    finding: Finding,
    violations_by_key: Mapping[_CoverKey, Violation],
) -> str:
    """Одна строка отчёта для `finding` (формат — докстринг `render_report`)."""
    parts = [finding.code]
    violation = violations_by_key.get((finding.rule, finding.site, finding.member))
    if violation is not None:
        parts.append(violation.kind)
    if finding.rule:
        parts.append(finding.rule)
    if finding.site:
        parts.append(finding.site)
    if finding.member is not None:
        parts.append(f"member={finding.member}")
    if violation is not None and violation.kind == _CONSTRUCT_ONLY_IN:
        parts.append(f"count={violation.count}")
    if finding.detail:
        parts.append(finding.detail)
    return " ".join(parts)
