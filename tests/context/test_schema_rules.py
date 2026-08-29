"""Тесты `schema_rules.py` — статический блок требований §4.4: TASK-003.

Модуля `disputatio.context.schema_rules` на red-чекпоинте ещё нет, поэтому
импорт на уровне модуля сорвал бы collection всего каталога (`error`, а не
честный assertion-red). Загрузка ленивая, через `_load_schema_rules`, а
`ImportError` переводится в `AssertionError`.

Что именно пинится. Текст блока — деталь реализации, и тест не должен
ломаться от переформулировки. Но каждое из четырёх правил §4.4 обязано
присутствовать как СВЯЗНОЕ утверждение, а не набором слов, рассыпанным по
абзацу: поэтому проверка ищет ровно одну строку, где встречаются все
опорные токены правила. Удаление любого правила валит свой тест поимённо,
а не один общий «текст изменился».

Констант две — develop-редакция и doc-редакция, различаются только тегом
схемы (`disputatio/v1` против `disputatio/v2`, §5.1 SPEC-002). Правила §4.4
проверяются на ОБЕИХ: doc-ревью судится теми же четырьмя правилами
(`runtime/steps.py::_accepted_review`), и блок, потерявший правило в одной
из редакций, обязан валить тест поимённо.

Отдельная тема — происхождение констант (два последних теста). Урок
TASK-001: значение, вычисленное НА ИМПОРТЕ (`uuid4()`, время, окружение),
внутри одного процесса неотличимо от литерала, и весь каталог на таком
мутанте зелёный. Поэтому байты сравниваются с двумя отдельными процессами,
а присваивание дополнительно разбирается через AST — там, где раньше
требовался голый литерал, теперь допускается ровно одна вычислимая форма:
подстановка тега в литерал-шаблон верхнего уровня.
"""

import ast
import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

CONSTANT_NAME = "REVIEW_SCHEMA_REQUIREMENTS"
DOC_CONSTANT_NAME = "DOC_REVIEW_SCHEMA_REQUIREMENTS"
BOTH_CONSTANTS = (CONSTANT_NAME, DOC_CONSTANT_NAME)

#: Модуль, откуда допустимо брать подставляемый тег схемы: теги — контракт
#: артефактов, а не текст промпта, и второй их копии здесь быть не должно.
_SCHEMA_TAG_SOURCE = "disputatio.contracts.base"

_PROBE = (
    "import sys;"
    "from disputatio.context import schema_rules;"
    "sys.stdout.buffer.write("
    "b'\\x00'.join(getattr(schema_rules, n).encode() for n in "
    f"{BOTH_CONSTANTS!r}))"
)


def _load_schema_rules() -> ModuleType:
    """Импортирует модуль; его отсутствие — assertion, а не collection error."""
    try:
        return importlib.import_module("disputatio.context.schema_rules")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.schema_rules не импортируется: {exc}"
        ) from exc


def _requirements_text(name: str = CONSTANT_NAME) -> str:
    """Значение константы; её отсутствие или не-строка — тоже assertion."""
    module = _load_schema_rules()
    assert hasattr(module, name), (
        f"в schema_rules.py нет константы {name} — "
        "ревьюеру нечего сообщить о схеме вывода (§4.4)"
    )
    text = getattr(module, name)
    assert isinstance(text, str), (
        f"{name} должен быть строкой, а не {type(text).__name__}: "
        "функция-обёртка не нужна, блок вставляется в промпт как есть"
    )
    return text


def _rule_line(text: str, *tokens: str) -> str:
    """Единственная строка, где встречаются ВСЕ `tokens` (регистронезависимо).

    «Ровно одна» — не придирка: два разных утверждения об одном правиле
    противоречили бы друг другу в промпте, а ноль означает, что правило
    рассыпалось по абзацу и связного требования ревьюер не увидит.
    """
    lowered = [token.lower() for token in tokens]
    matches = [
        line
        for line in text.splitlines()
        if all(token in line.lower() for token in lowered)
    ]
    assert len(matches) == 1, (
        f"ожидалась ровно одна строка с токенами {tokens}, найдено "
        f"{len(matches)}; правило §4.4 отсутствует или сформулировано "
        f"несвязно. Текст блока:\n{text}"
    )
    return matches[0]


_for_both = pytest.mark.parametrize("name", BOTH_CONSTANTS)


@_for_both
def test_constant_exists_and_is_non_empty_text(name: str) -> None:
    """Константа импортируема, это непустой текст, а не заглушка."""
    text = _requirements_text(name)

    assert text.strip(), f"{name} пуст — блок §4.4 не дойдёт до ревьюера"
    assert "review.json" in text, (
        f"{name} не называет артефакт, схему которого описывает"
    )


@pytest.mark.parametrize(
    ("name", "tag", "foreign_tag"),
    [
        (CONSTANT_NAME, "disputatio/v1", "disputatio/v2"),
        (DOC_CONSTANT_NAME, "disputatio/v2", "disputatio/v1"),
    ],
)
def test_block_names_the_only_accepted_schema_tag(
    name: str, tag: str, foreign_tag: str
) -> None:
    """Каждая редакция называет СВОЙ тег и не называет чужой (§5.1 SPEC-002).

    Тег — не украшение: `review.json` с `checklist` под `disputatio/v1`
    отвергается схемой (`contracts/review.py`), а без `schema` — вовсе не
    парсится. Редакция, назвавшая чужой тег, отправляет агента на
    гарантированный отказ валидации.
    """
    text = _requirements_text(name)

    assert tag in text, f"{name} не называет тег схемы {tag}"
    assert foreign_tag not in text, (
        f"{name} называет чужой тег {foreign_tag}: агент, послушавшийся "
        "промпта, получит отказ схемы"
    )


@_for_both
def test_rule_request_changes_requires_blocking_issue(name: str) -> None:
    """§4.4/1: `request_changes|reject` ⇒ непустой `issues` с ≥1 blocker|major."""
    text = _requirements_text(name)

    line = _rule_line(text, "request_changes", "reject", "issues", "blocker", "major")

    assert "непуст" in line.lower(), (
        f"правило не требует, чтобы `issues` был непуст: {line}"
    )


@_for_both
def test_rule_blocking_issue_requires_evidence(name: str) -> None:
    """§4.4/2: каждый blocker|major обязан нести непустой `evidence`."""
    text = _requirements_text(name)

    line = _rule_line(text, "evidence", "blocker", "major", "minor")

    assert "непуст" in line.lower(), (
        f"правило не требует непустого `evidence` у блокирующих замечаний: {line}"
    )


@_for_both
def test_rule_approve_forbidden_when_verification_failed(name: str) -> None:
    """§4.4/3: `approve` запрещён при `verification.overall == fail`."""
    text = _requirements_text(name)

    line = _rule_line(text, "approve", "verification.overall", "fail")

    assert "запрещ" in line.lower(), (
        f"правило не запрещает `approve` при красной верификации: {line}"
    )


@_for_both
def test_rule_checked_is_mandatory(name: str) -> None:
    """§4.4/4: `checked` обязателен — это прокси верифицируемости ревью."""
    text = _requirements_text(name)

    line = _rule_line(text, "checked", "обязателен")

    assert "checked" in line, f"правило про `checked` потеряло имя поля: {line}"


@_for_both
def test_checked_requirement_demands_non_empty_list(name: str) -> None:
    """Требование к `checked` явно про НЕПУСТОТУ, а не про наличие ключа.

    Отдельный тест от `test_rule_checked_is_mandatory`: «поле обязательно»
    ревьюер выполняет и пустым списком, а по §4.4 пустой список = ревью не
    принято, шаг повторяется. Именно эта половина требования и должна быть
    сказана вслух.
    """
    text = _requirements_text(name)

    line = _rule_line(text, "checked", "обязателен")

    assert "непуст" in line.lower(), (
        f"требование к `checked` не говорит о непустоте: {line}"
    )
    assert "пустой список" in line.lower(), (
        f"не сказано, чем грозит пустой `checked` — ревью не принято: {line}"
    )


def _module_bindings(source: str) -> tuple[dict[str, str], set[str]]:
    """Имена верхнего уровня: строковые литералы и импорты тегов схемы."""
    literals: dict[str, str] = {}
    tag_names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            if node.module == _SCHEMA_TAG_SOURCE:
                tag_names.update(alias.asname or alias.name for alias in node.names)
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target = node.targets[0].id
        else:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            literals[target] = value.value
    return literals, tag_names


def _assigned_value(source: str, name: str) -> ast.expr:
    """Выражение, присвоенное `name` на верхнем уровне модуля."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            return node.value
    raise AssertionError(f"{name} не присвоен на верхнем уровне schema_rules.py")


@_for_both
def test_constant_is_built_from_module_literals_only(name: str) -> None:
    """Значение собрано из литералов модуля — не вычислено из состояния.

    Раньше здесь требовался голый литерал. Требование ослаблено ровно на
    одну форму — `«литерал-шаблон».format(schema=<тег из contracts.base>)`,
    — и ослаблено осознанно: редакций блока стало две, а различаются они
    одним тегом схемы, и вторая полная копия текста была бы вторым местом
    правды для правил §4.4. Всё прочее по-прежнему запрещено: `f`-строка,
    вызов любой другой функции, имя, не связанное литералом на верхнем
    уровне, — каждая из этих форм пускает в блок состояние процесса.
    """
    module = _load_schema_rules()
    assert module.__file__ is not None, "у schema_rules нет исходника на диске"
    source = Path(module.__file__).read_text(encoding="utf-8")
    literals, tag_names = _module_bindings(source)
    value = _assigned_value(source, name)

    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        assert value.value == _requirements_text(name), (
            f"{name} во время выполнения не равен литералу из исходника"
        )
        return

    assert isinstance(value, ast.Call), (
        f"{name} присвоен формой {type(value).__name__}: допустимы только "
        "строковый литерал и подстановка тега в литерал-шаблон"
    )
    func = value.func
    assert isinstance(func, ast.Attribute) and func.attr == "format", (
        f"{name} вычисляется вызовом, отличным от `.format` — значение "
        "перестаёт быть воспроизводимым"
    )
    assert isinstance(func.value, ast.Name) and func.value.id in literals, (
        f"{name} подставляет в шаблон, который сам не является строковым "
        "литералом верхнего уровня"
    )
    assert not value.args, f"{name}: у `.format` только именованные аргументы"
    for keyword in value.keywords:
        arg = keyword.value
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            continue
        assert isinstance(arg, ast.Name) and arg.id in tag_names, (
            f"{name}: в шаблон подставляется {ast.dump(arg)} — допустимы "
            f"только строковый литерал и имя, импортированное из "
            f"{_SCHEMA_TAG_SOURCE}"
        )
        assert isinstance(getattr(module, arg.id), str), (
            f"{name}: подставляемый {arg.id} — не строка"
        )


def test_constants_are_byte_identical_across_processes() -> None:
    """NFR-002: одинаковые байты в разных процессах и после reload.

    Аргументов у констант нет по построению, поэтому «не зависит от
    аргументов» проверяется с другой стороны: значение не должно зависеть
    ни от чего вообще — ни от повторного выполнения кода модуля, ни от
    запуска интерпретатора. Это единственная проверка, которая ловит
    состояние, одинаковое внутри процесса, но разное между запусками.
    """
    expected = b"\x00".join(
        _requirements_text(name).encode() for name in BOTH_CONSTANTS
    )

    reloaded = importlib.reload(_load_schema_rules())
    after_reload = b"\x00".join(
        getattr(reloaded, name).encode() for name in BOTH_CONSTANTS
    )
    assert after_reload == expected, (
        "повторное выполнение кода модуля даёт другое значение — "
        "в модуле есть вычисляемое состояние"
    )

    runs = [
        subprocess.run(
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            check=False,
        )
        for _ in range(2)
    ]
    for run in runs:
        assert run.returncode == 0, (
            f"проба в отдельном процессе упала: {run.stderr.decode(errors='replace')}"
        )
    assert runs[0].stdout == runs[1].stdout
    assert runs[0].stdout == expected


def test_the_two_editions_differ_only_by_the_schema_tag() -> None:
    """Одно место правды: редакции совпадают после нормализации тега.

    Проверка не косметическая — она и есть страховка от второй копии
    текста: разойтись формулировкой правила §4.4 между develop- и
    doc-промптом значит судить два ревью по разным описаниям одних и тех
    же правил валидатора.
    """
    module = _load_schema_rules()
    develop = _requirements_text(CONSTANT_NAME).replace(module.SCHEMA_V1, "<tag>")
    doc = _requirements_text(DOC_CONSTANT_NAME).replace(module.SCHEMA_V2, "<tag>")

    assert develop == doc, (
        "редакции блока §4.4 разошлись не только тегом схемы — "
        "формулировки правил обязаны жить в одном месте"
    )
