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

Отдельная тема — происхождение константы (последний тест). Урок TASK-001:
значение, вычисленное НА ИМПОРТЕ (`uuid4()`, время, окружение), внутри
одного процесса неотличимо от литерала, и весь каталог на таком мутанте
зелёный. Поэтому байты сравниваются с двумя отдельными процессами, а
присваивание дополнительно разбирается через AST.
"""

import ast
import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

CONSTANT_NAME = "REVIEW_SCHEMA_REQUIREMENTS"

_PROBE = (
    "import sys;"
    "from disputatio.context.schema_rules import REVIEW_SCHEMA_REQUIREMENTS;"
    "sys.stdout.buffer.write(REVIEW_SCHEMA_REQUIREMENTS.encode())"
)


def _load_schema_rules() -> ModuleType:
    """Импортирует модуль; его отсутствие — assertion, а не collection error."""
    try:
        return importlib.import_module("disputatio.context.schema_rules")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.schema_rules не импортируется: {exc}"
        ) from exc


def _requirements_text() -> str:
    """Значение константы; её отсутствие или не-строка — тоже assertion."""
    module = _load_schema_rules()
    assert hasattr(module, CONSTANT_NAME), (
        f"в schema_rules.py нет константы {CONSTANT_NAME} — "
        "ревьюеру нечего сообщить о схеме вывода (§4.4)"
    )
    text = getattr(module, CONSTANT_NAME)
    assert isinstance(text, str), (
        f"{CONSTANT_NAME} должен быть строкой, а не {type(text).__name__}: "
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


def test_constant_exists_and_is_non_empty_text() -> None:
    """Константа импортируема, это непустой текст, а не заглушка."""
    text = _requirements_text()

    assert text.strip(), f"{CONSTANT_NAME} пуст — блок §4.4 не дойдёт до ревьюера"
    assert "review.json" in text, (
        f"{CONSTANT_NAME} не называет артефакт, схему которого описывает"
    )


def test_rule_request_changes_requires_blocking_issue() -> None:
    """§4.4/1: `request_changes|reject` ⇒ непустой `issues` с ≥1 blocker|major."""
    text = _requirements_text()

    line = _rule_line(text, "request_changes", "reject", "issues", "blocker", "major")

    assert "непуст" in line.lower(), (
        f"правило не требует, чтобы `issues` был непуст: {line}"
    )


def test_rule_blocking_issue_requires_evidence() -> None:
    """§4.4/2: каждый blocker|major обязан нести непустой `evidence`."""
    text = _requirements_text()

    line = _rule_line(text, "evidence", "blocker", "major", "minor")

    assert "непуст" in line.lower(), (
        f"правило не требует непустого `evidence` у блокирующих замечаний: {line}"
    )


def test_rule_approve_forbidden_when_verification_failed() -> None:
    """§4.4/3: `approve` запрещён при `verification.overall == fail`."""
    text = _requirements_text()

    line = _rule_line(text, "approve", "verification.overall", "fail")

    assert "запрещ" in line.lower(), (
        f"правило не запрещает `approve` при красной верификации: {line}"
    )


def test_rule_checked_is_mandatory() -> None:
    """§4.4/4: `checked` обязателен — это прокси верифицируемости ревью."""
    text = _requirements_text()

    line = _rule_line(text, "checked", "обязателен")

    assert "checked" in line, f"правило про `checked` потеряло имя поля: {line}"


def test_checked_requirement_demands_non_empty_list() -> None:
    """Требование к `checked` явно про НЕПУСТОТУ, а не про наличие ключа.

    Отдельный тест от `test_rule_checked_is_mandatory`: «поле обязательно»
    ревьюер выполняет и пустым списком, а по §4.4 пустой список = ревью не
    принято, шаг повторяется. Именно эта половина требования и должна быть
    сказана вслух.
    """
    text = _requirements_text()

    line = _rule_line(text, "checked", "обязателен")

    assert "непуст" in line.lower(), (
        f"требование к `checked` не говорит о непустоте: {line}"
    )
    assert "пустой список" in line.lower(), (
        f"не сказано, чем грозит пустой `checked` — ревью не принято: {line}"
    )


def test_constant_is_a_plain_string_literal() -> None:
    """Константа присвоена литералом верхнего уровня — не вычислена.

    `f`-строка, вызов или имя справа от `=` означают, что значение зависит
    от состояния процесса; в результат такое присваивание не попадает вовсе
    и тест падает на проверке наличия имени.
    """
    module = _load_schema_rules()
    assert module.__file__ is not None, "у schema_rules нет исходника на диске"
    source = Path(module.__file__).read_text(encoding="utf-8")

    literals: dict[str, str] = {}
    for node in ast.parse(source).body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target = node.targets[0].id
        if target is None or not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            literals[target] = value.value

    assert CONSTANT_NAME in literals, (
        f"{CONSTANT_NAME} присвоен не строковым литералом верхнего уровня — "
        "значение вычисляется, блок перестаёт быть воспроизводимым"
    )
    assert literals[CONSTANT_NAME] == _requirements_text(), (
        f"{CONSTANT_NAME} во время выполнения не равен литералу из исходника"
    )


def test_constant_is_byte_identical_across_processes() -> None:
    """NFR-002: одинаковые байты в разных процессах и после reload.

    Аргументов у константы нет по построению, поэтому «не зависит от
    аргументов» проверяется с другой стороны: значение не должно зависеть
    ни от чего вообще — ни от повторного выполнения кода модуля, ни от
    запуска интерпретатора.
    """
    expected = _requirements_text().encode()

    reloaded = importlib.reload(_load_schema_rules())
    assert getattr(reloaded, CONSTANT_NAME).encode() == expected, (
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
