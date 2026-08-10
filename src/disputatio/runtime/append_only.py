"""AST-скан гарантий [DESIGN-016] — иммутабельность и append-only ([REQ-016]).

Обе гарантии раунда обеспечиваются не проверкой во время работы, а формой
кода runtime, и увидеть их можно только в тексте:

* **I3 не глушится.** Тихая правка финализированного раунда хуже падения,
  поэтому `except`, поглотивший `RoundImmutableError`, — дефект, которого
  один прогон не покажет: сценарий, где раунд ещё не закрыт, зеленеет и с
  ним, и без него.
* **Журнал пишет только `JsonlEventSink`.** `events.jsonl` дописывается
  `O_APPEND`-ом и ничем больше; любой второй писатель в runtime — это
  усечение журнала, которое подписчик §8 заметит уже после потери событий.

Сканер живёт в `src`, а не под `tests/` ([ADR-006]): граница здесь —
свойство пакета, а не тестовая утилита, и анализатор внутри `tests/`
сделал бы red-чекпоинт нечестным.

Строковые литералы-выражения (docstring'и модулей, функций и атрибутов) для
скана — проза: `events.jsonl` называют почти все модули цикла, и запрет
упоминать журнал в документации превратил бы проверку в требование молчать о
нём. Значение имеет литерал, попавший в вычисление, — из него строят путь.
"""

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

JOURNAL_FILE_NAME: Final = "events.jsonl"
"""Имя журнала событий; в вычислениях runtime его быть не должно."""

JOURNAL_PATH_HELPER: Final = "events_jsonl_path"
"""Построитель пути журнала из `disputatio.events.paths` (§3)."""

IMMUTABILITY_ERROR: Final = "RoundImmutableError"
"""Исключение I3, которое runtime не вправе поглощать ([REQ-009])."""

BROAD_EXCEPTIONS: Final = frozenset({"Exception", "BaseException"})
"""Типы, ловящие `RoundImmutableError` заодно со всем остальным."""

WRITE_CALL_NAMES: Final = frozenset(
    {
        "open",
        "fdopen",
        "write",
        "writelines",
        "write_text",
        "write_bytes",
        "truncate",
        "unlink",
        "remove",
        "rename",
        "replace",
        "rmtree",
        "mkstemp",
    }
)
"""Имена вызовов, способных создать, перезаписать или укоротить файл.

Список намеренно шире журнала: писатель `events.jsonl` появляется не под
своим именем, а как обычный `open`/`write_text` по собранному пути, и узнать
его по имени переменной нельзя. Поэтому скан отдаёт ВСЕ такие вызовы, а
решение «этот законен» принимает тест списком-разрешением — новый писатель в
runtime обязан быть замечен, даже если пишет он не в журнал.
"""

KIND_LITERAL: Final = "journal-literal"
KIND_PATH_HELPER: Final = "journal-path-helper"


@dataclass(frozen=True, slots=True)
class JournalReference:
    """Упоминание журнала в вычислении: модуль, строка, что именно, вид."""

    module: Path
    lineno: int
    target: str
    kind: str


@dataclass(frozen=True, slots=True)
class FileWrite:
    """Вызов, способный записать файл: модуль, строка, вызываемое, режим."""

    module: Path
    lineno: int
    target: str
    mode: str


@dataclass(frozen=True, slots=True)
class ImmutabilityHandler:
    """`except`, ловящий I3: где, в какой функции, что ловит, поднимает ли."""

    module: Path
    lineno: int
    function: str
    caught: str
    reraises: bool


def scan_journal_references(package_dir: Path) -> list[JournalReference]:
    """Упоминания `events.jsonl` вне прозы во всех модулях `package_dir`.

    Два вида: литерал с именем журнала (`kind == KIND_LITERAL`) и обращение
    к построителю пути `events_jsonl_path` — импортом или вызовом
    (`kind == KIND_PATH_HELPER`). Первое ловит путь, собранный руками,
    второе — собранный правильно, но не тем, кому это позволено: журнал
    открывает `JsonlEventSink`, и только он.
    """
    return _scan(package_dir, _journal_references)


def scan_file_writes(package_dir: Path) -> list[FileWrite]:
    """Все вызовы `package_dir`, способные записать или удалить файл.

    Чтение не считается: `open` с литеральным режимом без `w`/`a`/`x`/`+`
    пропускается, отсутствие режима — это `"r"`. Нелитеральный режим
    считается записью: чем гадать о значении переменной, дешевле показать
    вызов тесту.
    """
    return _scan(package_dir, _file_writes)


def scan_immutability_handlers(package_dir: Path) -> list[ImmutabilityHandler]:
    """Все `except`, способные поймать `RoundImmutableError`.

    Считаются три формы: явное имя исключения (в том числе в кортеже),
    голый `except:` и широкий `except Exception`/`BaseException` — последние
    ловят I3 заодно, и молчаливость от этого не становится осознанной.
    `reraises` отвечает только на вопрос «есть ли в обработчике `raise`»:
    условный повторный подъём (`_write_decision`, [REQ-015]) — законная
    терпимость к повтору шага, поглощение без единого `raise` — нет.
    """
    return _scan(package_dir, _immutability_handlers)


def swallowed_immutability(package_dir: Path) -> list[ImmutabilityHandler]:
    """Обработчики I3 без единого `raise` — то есть тихая правка раунда."""
    return [
        handler
        for handler in scan_immutability_handlers(package_dir)
        if not handler.reraises
    ]


def _scan[T](
    package_dir: Path, collect: Callable[[ast.AST, Path], Iterator[T]]
) -> list[T]:
    """Применяет сборщик `collect` к каждому модулю дерева, в порядке путей.

    Собственный модуль из обхода исключён по абсолютному пути: имя журнала и
    имя исключения I3 — предмет проверки, и записать их без литерала нельзя.
    Исключение именно этого файла, а не имени `append_only.py`: копия дерева,
    сделанная тестом, обязана сканироваться целиком.
    """
    found: list[T] = []
    for path in sorted(package_dir.rglob("*.py")):
        if path.resolve() == _SELF:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend(collect(tree, path))
    return found


def _journal_references(tree: ast.AST, module: Path) -> Iterator[JournalReference]:
    """Упоминания журнала одного модуля, в порядке обхода AST."""
    prose = _prose_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if id(node) in prose or not isinstance(node.value, str):
                continue
            if JOURNAL_FILE_NAME in node.value:
                yield JournalReference(module, node.lineno, node.value, KIND_LITERAL)
        elif isinstance(node, ast.Name) and node.id == JOURNAL_PATH_HELPER:
            yield JournalReference(module, node.lineno, node.id, KIND_PATH_HELPER)
        elif isinstance(node, ast.Attribute) and node.attr == JOURNAL_PATH_HELPER:
            yield JournalReference(
                module, node.lineno, ast.unparse(node), KIND_PATH_HELPER
            )
        elif isinstance(node, ast.ImportFrom):
            yield from _imported_helper(node, module)


def _imported_helper(node: ast.ImportFrom, module: Path) -> Iterator[JournalReference]:
    """`from … import events_jsonl_path` — обращение к построителю пути."""
    for alias in node.names:
        if alias.name == JOURNAL_PATH_HELPER:
            yield JournalReference(module, node.lineno, alias.name, KIND_PATH_HELPER)


def _file_writes(tree: ast.AST, module: Path) -> Iterator[FileWrite]:
    """Пишущие вызовы одного модуля, в порядке обхода AST."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node.func)
        if name not in WRITE_CALL_NAMES:
            continue
        mode = _mode_argument(node, name)
        if name in _MODED_CALLS and not _mode_writes(mode):
            continue
        yield FileWrite(module, node.lineno, ast.unparse(node.func), mode)


def _immutability_handlers(
    tree: ast.AST, module: Path
) -> Iterator[ImmutabilityHandler]:
    """Обработчики I3 одного модуля вместе с именем объемлющей функции."""
    catching = _catching_names(tree)
    for function, handler in _handlers(tree, function=""):
        caught = "" if handler.type is None else ast.unparse(handler.type)
        if not _catches_immutability(handler.type, catching):
            continue
        yield ImmutabilityHandler(
            module=module,
            lineno=handler.lineno,
            function=function,
            caught=caught,
            reraises=any(
                isinstance(inner, ast.Raise)
                for statement in handler.body
                for inner in ast.walk(statement)
            ),
        )


def _handlers(
    node: ast.AST, *, function: str
) -> Iterator[tuple[str, ast.ExceptHandler]]:
    """Пары «объемлющая функция, обработчик» — обход с переносом имени.

    Имя берётся у ближайшей функции, а не у модуля: обработчик опознаётся в
    отчёте по месту, а модуль цикла содержит десяток функций, и «steps.py»
    без имени не сказал бы, какая из них молчит.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from _handlers(child, function=child.name)
            continue
        if isinstance(child, ast.ExceptHandler):
            yield function, child
        yield from _handlers(child, function=function)


def _catches_immutability(caught: ast.expr | None, catching: frozenset[str]) -> bool:
    """`True`, если такой `except` поймает `RoundImmutableError`.

    Сверка идёт со списком имён модуля (`catching`), а не с одним именем
    исключения: `except` называет локальную привязку, и она не обязана
    совпадать с именем класса.
    """
    if caught is None:
        return True
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(_called_name(name) in catching for name in names)


def _catching_names(tree: ast.AST) -> frozenset[str]:
    """Имена, за которыми в `except` этого модуля стоит `RoundImmutableError`.

    Кроме самого `RoundImmutableError` и широких типов сюда попадают его
    локальные привязки: `import … as`, переименование присваиванием и кортеж
    исключений, собранный в константу. Без них скан проверял бы написание, а
    не смысл: `except _RIE` и `except IGNORED`, где `IGNORED =
    (RoundImmutableError,)`, глушат I3 ровно так же, как `except
    RoundImmutableError`, — а разница в одну строку импорта.

    У присваивания смотрится ТОЛЬКО значение: аннотация вида `tuple[type[
    Exception], …]` называет широкий тип, ничего им не ловя, и учёт аннотаций
    объявил бы обработчиком I3 всякий `except` по такой константе — включая
    schema-retry ([DESIGN-006]), который ловит совсем другое.
    """
    names = set(_CATCHING_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names |= _rebound_by_import(node, names)
    while (rebound := _rebound_by_assignment(tree, names)) - names:
        names |= rebound
    return frozenset(names)


def _rebound_by_import(node: ast.Import | ast.ImportFrom, names: set[str]) -> set[str]:
    """Локальные имена импорта, за которыми стоит уже известное имя."""
    return {
        alias.asname or alias.name
        for alias in node.names
        if alias.name.rpartition(".")[2] in names
    }


def _rebound_by_assignment(tree: ast.AST, names: set[str]) -> set[str]:
    """Имена, которым присвоено значение, упоминающее известное имя."""
    rebound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not _mentions(value, names):
            continue
        rebound |= {target.id for target in targets if isinstance(target, ast.Name)}
    return rebound


def _mentions(value: ast.expr, names: set[str]) -> bool:
    """`True`, если выражение называет одно из имён `names`."""
    return any(
        _called_name(node) in names
        for node in ast.walk(value)
        if isinstance(node, ast.Name | ast.Attribute)
    )


def _prose_nodes(tree: ast.AST) -> frozenset[int]:
    """Идентификаторы строковых литералов-выражений — docstring'и и прочая проза."""
    return frozenset(
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _mode_argument(node: ast.Call, name: str) -> str:
    """Режим открытия — как он записан в коде; позиция зависит от вызываемого.

    У встроенного `open(path, "a")` режим второй, у метода `path.open("a")` —
    первый. Спутай их, и `Path.open("a")` прочитался бы как открытие без
    режима, то есть как чтение: писатель журнала прошёл бы скан насквозь.

    `os.open` — тоже метод по форме, но второй его аргумент не режим, а флаги,
    и первый — путь. Считать путь режимом опаснее всего: `os.open("log.jsonl",
    O_APPEND)` не содержит ни одной буквы записи, то есть прочитался бы как
    чтение. Поэтому у него берётся второй аргумент — нелитеральные флаги скан
    трактует как запись.
    """
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return ast.unparse(keyword.value)
    index = 0 if _is_bound_open(node.func, name) else 1
    return ast.unparse(node.args[index]) if len(node.args) > index else ""


def _is_bound_open(func: ast.expr, name: str) -> bool:
    """`True` для `something.open(mode)`, где `something` — не модуль `os`."""
    if name != "open" or not isinstance(func, ast.Attribute):
        return False
    receiver = func.value
    return not (isinstance(receiver, ast.Name) and receiver.id == _OS_MODULE)


def _mode_writes(mode: str) -> bool:
    """`True`, если режим открытия допускает запись либо неизвестен."""
    if not mode:
        return False
    literal = ast.literal_eval(mode) if _is_string_literal(mode) else None
    if not isinstance(literal, str):
        return True
    return any(flag in literal for flag in "wax+")


def _is_string_literal(source: str) -> bool:
    """`True`, если исходник аргумента — строковый литерал, а не выражение."""
    try:
        return isinstance(ast.literal_eval(source), str)
    except (ValueError, SyntaxError):
        return False


def _called_name(func: ast.expr) -> str:
    """Последнее имя вызываемого: и для `Name`, и для `Attribute`."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


_CATCHING_NAMES: Final = frozenset({IMMUTABILITY_ERROR}) | BROAD_EXCEPTIONS
"""Имена в `except`, после которых I3 оказывается пойманным."""

_SELF: Final = Path(__file__).resolve()
"""Путь самого сканера — единственный модуль runtime вне обхода."""

_MODED_CALLS: Final = frozenset({"open", "fdopen"})
"""Вызовы, у которых запись отличается от чтения режимом, а не именем."""

_OS_MODULE: Final = "os"
"""Получатель, у которого `open` принимает путь и флаги, а не режим."""
