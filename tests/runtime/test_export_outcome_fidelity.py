"""Исход в манифесте перенесён из решения, а не назван ([REQ-017]).

[TASK-018], дополнение к `test_export_converged.py`. Тот наблюдает экспорт
только сошедшейся сессии, где исход раунда-источника один и тот же во всех
сценариях: `manifest["outcome"]`, заменённый на литерал `"converged"`,
проходит его набор целиком (проверено подменой). Поведенческая половина
этого пина приходит вместе с веткой эскалации ([TASK-019],
`test_export_partial.py`) — там исход другой, и константа станет видна
сразу.

До тех пор дыра закрывается формой: кодов исходов §5 в модуле экспорта нет
как значений. Ключи словаря из проверки исключены — `"converged"` в
манифесте это ИМЯ поля §3.2, а не исход; запрещено ровно то место, где код
исхода мог бы оказаться ответом на вопрос «чем кончилась сессия».
Отсутствующий литерал обойти нечем, а перечень поведений обходится новой
веткой.
"""

import ast
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from types import ModuleType

from disputatio.contracts import Outcome

_OUTCOME_CODES = frozenset(outcome.value for outcome in Outcome)


def _exporting() -> ModuleType:
    """Модуль `runtime/exporting.py`; отсутствие — `AssertionError`."""
    try:
        return import_module("disputatio.runtime.exporting")
    except ImportError as exc:  # pragma: no cover - ветка красного чекпоинта
        raise AssertionError(f"нет модуля runtime/exporting.py: {exc}") from exc


def _source(module: ModuleType) -> str:
    """Исходник модуля с диска — для проверок «код такой формы отсутствует»."""
    path = module.__file__
    assert path is not None, f"у {module.__name__} нет файла на диске"
    return Path(path).read_text(encoding="utf-8")


def _functions_reachable_from(
    tree: ast.Module, root: str
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Функция `root` модуля и все функции модуля, вызываемые из неё.

    Замыкание транзитивное: код, вынесенный в хелпер, скан обязан видеть так
    же, как код в теле, — иначе запрет обходится одной лишней функцией.
    """
    defined: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert root in defined, f"модуль не определяет функцию {root!r}"

    seen: set[str] = set()
    queue = [root]
    while queue:
        current = queue.pop()
        if current in seen or current not in defined:
            continue
        seen.add(current)
        for node in ast.walk(defined[current]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                queue.append(node.func.id)
    return [defined[name] for name in sorted(seen)]


def _value_strings(nodes: Sequence[ast.AST]) -> set[str]:
    """Строковые константы `nodes`, кроме ключей словарей и докстрок.

    Ключ словаря — имя поля манифеста, докстрока — объяснение; ни то, ни
    другое ответом на вопрос «чем кончилась сессия» быть не может. Всё
    остальное — значение, и именно там литерал подменил бы собой решение.
    """
    ignored: set[int] = set()
    for scope in nodes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Dict):
                ignored.update(id(key) for key in node.keys if key is not None)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                ignored.add(id(node.value))

    values: set[str] = set()
    for scope in nodes:
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in ignored
            ):
                values.add(node.value)
    return values


def test_export_never_spells_out_an_outcome_code() -> None:
    """Ни один код исхода §5 не встречается в экспорте значением.

    `manifest["outcome"]` и `manifest["stop_reason"]` — перенос того, что
    §5 уже записал в `decision.json`. Литерал на их месте пережил бы всю
    сошедшуюся ветку и соврал бы ровно в тот раз, когда сессия кончилась
    иначе.
    """
    module = _exporting()
    functions = _functions_reachable_from(ast.parse(_source(module)), "export")

    leaked = sorted(_value_strings(list(functions)) & _OUTCOME_CODES)

    assert not leaked, (
        f"runtime/exporting.py называет исход строкой: {leaked} — исход "
        "берётся у решения раунда-источника, а не сочиняется экспортом"
    )


def test_the_scan_sees_a_planted_outcome_literal() -> None:
    """Обратная половина: скан ловит подложенный литерал, а не молчит всегда.

    Без неё предыдущий тест был бы вакуумен — пустой результат `_value_strings`
    прошёл бы его при любой реализации экспорта.
    """
    tree = ast.parse(
        "def export(ctx):\n"
        '    """Докстрока со словом converged."""\n'
        "    return _manifest()\n"
        "\n"
        "def _manifest():\n"
        '    return {"outcome": "deadlock", "converged": False}\n'
    )
    functions = _functions_reachable_from(tree, "export")

    values = _value_strings(list(functions))

    assert values & _OUTCOME_CODES == {"deadlock"}
