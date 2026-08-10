"""Исход в манифесте перенесён из решения, а не назван ([REQ-017]).

[TASK-018], дополнение к `test_export_converged.py`. Тот наблюдает экспорт
только сошедшейся сессии, где исход раунда-источника один и тот же во всех
сценариях: `manifest["outcome"]`, заменённый на литерал `"converged"`,
проходит его набор целиком (проверено подменой). Поведенческая половина
этого пина приходит вместе с веткой эскалации ([TASK-019],
`test_export_partial.py`) — там исход другой, и константа станет видна
сразу.

До тех пор дыра закрывается формой: кодов исходов §5 в модуле экспорта нет
как значений. Скан идёт по ВСЕМУ модулю, а не по замыканию `export`:
константа уровня модуля — самая дешёвая форма подмены (рядом уже лежат
`RESULT_MD_NAME` и `RESULT_PATCH_NAME`), а из тела функции она видна одним
безобидным именем, которое запретить нечем.

Ключи словарей и докстроки из проверки исключены — `"converged"` в
манифесте это ИМЯ поля §3.2, а докстрока объяснение; ни то, ни другое
ответом на вопрос «чем кончилась сессия» быть не может. Запрещено ровно то
место, где код исхода мог бы оказаться этим ответом: отсутствующий литерал
обойти нечем, а перечень поведений обходится новой веткой.
"""

import ast
from importlib import import_module
from pathlib import Path
from types import ModuleType

from disputatio.contracts import Outcome

_OUTCOME_CODES = frozenset(outcome.value for outcome in Outcome)

# Подложенный модуль для обратной половины скана: по одному коду исхода в
# каждом месте, которое скан обязан различать.
_PLANTED_MODULE = (
    '"""converged"""\n'
    '_CODE = "budget_hit"\n'
    "\n"
    "def export(ctx):\n"
    '    """deadlock"""\n'
    '    return {"converged": _CODE, "outcome": "continue"}\n'
)


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


def _value_strings(tree: ast.Module) -> set[str]:
    """Строковые константы модуля, кроме ключей словарей и докстрок.

    Ключ словаря — имя поля манифеста, докстрока — объяснение; ни то, ни
    другое ответом на вопрос «чем кончилась сессия» быть не может. Всё
    остальное — значение, и именно там литерал подменил бы собой решение,
    на каком бы уровне модуля он ни лежал.
    """
    ignored: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            ignored.update(id(key) for key in node.keys if key is not None)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            ignored.add(id(node.value))

    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in ignored
    }


def test_export_never_spells_out_an_outcome_code() -> None:
    """Ни один код исхода §5 не встречается в экспорте значением.

    `manifest["outcome"]` и `manifest["stop_reason"]` — перенос того, что
    §5 уже записал в `decision.json`. Литерал на их месте пережил бы всю
    сошедшуюся ветку и соврал бы ровно в тот раз, когда сессия кончилась
    иначе.
    """
    tree = ast.parse(_source(_exporting()))
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "export"
        for node in tree.body
    ), "runtime/exporting.py не определяет функцию export"

    leaked = sorted(_value_strings(tree) & _OUTCOME_CODES)

    assert not leaked, (
        f"runtime/exporting.py называет исход строкой: {leaked} — исход "
        "берётся у решения раунда-источника, а не сочиняется экспортом"
    )


def test_the_scan_sees_a_planted_outcome_literal() -> None:
    """Обратная половина: скан ловит подложенные литералы, а не молчит всегда.

    Без неё предыдущий тест был бы вакуумен — пустой результат
    `_value_strings` прошёл бы его при любой реализации экспорта. Модуль
    подложен так, что тест различает все четыре места сразу: константа
    уровня модуля и литерал в теле функции обязаны попасть в результат,
    ключ словаря и обе докстроки — нет.
    """
    values = _value_strings(ast.parse(_PLANTED_MODULE))

    assert values & _OUTCOME_CODES == {"budget_hit", "continue"}
