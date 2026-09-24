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

Отдельно — `task_sections` (§5.3): заголовки задач плана (`### Задача N:` /
`### Task N:`) и границы их разделов, нужные для проверки «раздел задачи
ссылается на место нарушения».
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from disputatio.verifier.wiring_snapshot import WiringInputError


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
_RULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_CONSTRUCT_ONLY_IN = "construct-only-in"
_ENUMERATES_ALL = "enumerates-all"

_CONSTRUCT_RULE_FIELDS = frozenset({"id", "kind", "class", "allowed"})
_ENUMERATE_RULE_FIELDS = frozenset(
    {"id", "kind", "module", "function", "checkers", "members"}
)


def parse_rules(spec_text: str) -> tuple[Rule, ...]:
    """Правила из единственного блока `disputatio-wiring` спеки (§3.1).

    Блок обязан существовать в единственном числе и нести непустой массив
    `rule`: пустой или отсутствующий массив дал бы зелёный гейт без единой
    проверки, а обязательность блока тогда ничего бы не значила.
    """
    document = _load_block(spec_text, "disputatio-wiring", "спека")
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


def parse_cover(plan_text: str) -> CoverBlock:
    """Блок покрытия из единственного блока `disputatio-wiring-cover` плана (§5.1)."""
    document = _load_block(plan_text, "disputatio-wiring-cover", "план")
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


# Заголовок 1–3 уровня: `#`/`##`/`###`, пробел, непустой текст. Ровно столько
# `#`, сколько в группе: следующий символ обязан быть пробелом, поэтому
# `####...` группу 1–3 не матчит (после трёх `#` идёт четвёртый `#`, а не
# пробел) — уровень 4+ разделы не обрывает (§5.3).
_HEADING_RE = re.compile(r"^#{1,3} \S")
# Заголовок задачи — `### Задача N:` или `### Task N:` (§5.3), допускается
# любой из уровней 1–3, как и у прочих заголовков-границ раздела.
_TASK_HEADING_RE = re.compile(r"^#{1,3} (?:Задача|Task) (\d+)\s*:")
# Строка фенса — 3 и более обратных кавычки в начале строки; используется
# только чтобы не принять TOML-комментарий (`# ...`) внутри блока покрытия
# за заголовок markdown.
_FENCE_LINE_RE = re.compile(r"^`{3,}")


def task_sections(plan_text: str) -> Mapping[int, str]:
    """Разделы задач плана по номеру заголовка (§5.3).

    Заголовок задачи — `### Задача N:` или `### Task N:`; раздел — текст от
    заголовка до следующего заголовка уровня 1–3 (не обрывается `####`).
    Строки внутри fenced-блоков заголовками не считаются: иначе TOML-комментарий
    (`# ...`) блока покрытия читался бы как markdown-заголовок.
    """
    lines = plan_text.splitlines()
    heading_lines: list[int] = []
    task_headings: dict[int, int] = {}
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not _HEADING_RE.match(line):
            continue
        heading_lines.append(index)
        task_match = _TASK_HEADING_RE.match(line)
        if task_match:
            task_headings[index] = int(task_match.group(1))

    sections: dict[int, str] = {}
    for heading_index, task_number in task_headings.items():
        if task_number in sections:
            raise WiringInputError(
                f"план: несколько заголовков задачи №{task_number} (§5.3)"
            )
        end = next((h for h in heading_lines if h > heading_index), len(lines))
        sections[task_number] = "\n".join(lines[heading_index:end])
    return sections


# --- fenced-блоки ------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^(`{3,})(.*)$")


def _find_fenced_blocks(text: str, info: str) -> list[str]:
    """Содержимое fenced-блоков с info-строкой ровно `info` (§3.1/§5.1).

    Ищется построчно: открывающая строка — 3+ обратных кавычки с info-строкой
    после них, закрывающая — строка из одних обратных кавычек (без info).
    Markdown-фенсы не вкладываются: открывающая строка, встреченная внутри уже
    открытого блока, — литеральный текст, а не новое открытие, и текущий блок
    закрывается только «голой» строкой из кавычек.
    """
    blocks: list[str] = []
    in_fence = False
    capturing = False
    current: list[str] = []
    for line in text.splitlines():
        match = _FENCE_OPEN_RE.match(line)
        if not in_fence:
            if match:
                in_fence = True
                capturing = match.group(2).strip() == info
                current = []
            continue
        if match and match.group(2).strip() == "":
            if capturing:
                blocks.append("\n".join(current))
            in_fence = False
            capturing = False
            continue
        if capturing:
            current.append(line)
    return blocks


def _load_block(text: str, info: str, document_name: str) -> dict[str, object]:
    """Единственный блок `info` документа `text`, разобранный как TOML."""
    blocks = _find_fenced_blocks(text, info)
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
    if not isinstance(value, str) or not _RULE_ID_RE.match(value):
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
    return tuple(value)


def _parse_class_field(value: str, context: str) -> tuple[str, str]:
    """`модуль:Имя` (§3.2): ровно один `:`, qualname без точки (v1: только классы верхнего уровня)."""
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
