"""RED — TASK-002 (WS-disputatio-65): semantic diff между immutable-проекциями.

BEH-02 требует, чтобы `resume` канонизировал живую и ожидаемую модель ОДНОЙ
и той же версией канонизации так, чтобы форматирование конфига (комментарии,
пробелы, стиль кавычек, порядок незначимых TOML-таблиц, явная запись
значения, равного default) никогда не регистрировалось как semantic drift
(FR-02). Сегодня в `pipeline_semantic_proof` есть только `build_projection`
(строит проекцию ОДНОГО разобранного конфига, BEH-01) и `load_semantic_proof`
(восстанавливает proof как самостоятельный артефакт, BEH-12/13/15) — ни одна
функция не отвечает на вопрос «представляют ли эти две канонические проекции
одну и ту же immutable-модель?». Эта функция (`diff_projections`) — предмет
TASK-002 и фундамент BEH-02/04-07/14/19; её пока не существует.
"""

from pathlib import Path

from disputatio.runtime import pipeline_semantic_proof as psp
from disputatio.runtime.pipeline_config import load_pipeline_config

_BASELINE_TOML = """
[pipeline]
spec_path = "docs/spec.md"
plan_path = "docs/plan.md"
"""

# Семантически тот же пайплайн: комментарии, лишние пробелы, литеральные
# кавычки вместо базовых строк, незначимый порядок TOML-таблиц (пустые
# override-таблицы чеклистов объявлены раньше `[pipeline]`) и явная запись
# `max_architectural_returns`, равная действующему default'у (BEH-02).
_EQUIVALENT_TOML = """
# то же самое, другими словами
[pipeline.checklists.pair]
[pipeline.checklists.spec]

[pipeline]
plan_path    =    'docs/plan.md'   # literal-строка, не basic
spec_path = "docs/spec.md"
max_architectural_returns = 2
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_equivalent_toml_representations_diff_to_no_drift(tmp_path: Path) -> None:
    baseline = load_pipeline_config(_write(tmp_path, "baseline.toml", _BASELINE_TOML))
    equivalent = load_pipeline_config(
        _write(tmp_path, "equivalent.toml", _EQUIVALENT_TOML)
    )

    expected = psp.build_projection(baseline)
    live = psp.build_projection(equivalent)

    assert psp.diff_projections(expected, live) == []
