"""Две формы секции `[pipeline]` и вывод вида: SPEC-002 v0.2 §3.2, C3, §5.3.

Форма конфига И ЕСТЬ объявление вида (P0), поэтому разбор здесь fail-closed
и без «побеждает первый»: смешанная форма, пара наполовину, отсутствие всех
ключей и ключ чужой формы — четыре отдельных отказа, и текст каждого обязан
назвать ОБЕ допустимые схемы (C3).
"""

from pathlib import Path

import pytest

from disputatio.contracts.pipeline import PipelineKind
from disputatio.runtime.errors import ConfigError
from disputatio.runtime.pipeline_config import load_pipeline_config

_AGENTS = """
[agents.author]
adapter = "fake"
model = "m"
[agents.reviewer]
adapter = "fake"
model = "m"
[limits]
max_rounds = 3
max_total_tokens = 1000
max_wall_seconds = 60
schema_retries = 2
"""

_DOC_CHECKLIST = """
[pipeline.checklists.doc]
findings_item = "B1"
[pipeline.checklists.doc.items]
B1 = "нет blocker/major-находок"
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "disputatio.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --- вывод вида по форме (§3.2, P0) -----------------------------------


def test_document_form_yields_document_kind(tmp_path: Path) -> None:
    config = load_pipeline_config(
        _write(
            tmp_path,
            '[pipeline]\ndocument_path = "docs/charter.md"\n'
            + _DOC_CHECKLIST
            + _AGENTS,
        )
    )
    assert config.kind is PipelineKind.DOCUMENT
    assert config.document_path is not None
    assert config.document_path.as_posix() == "docs/charter.md"
    assert config.spec_path is None and config.plan_path is None


def test_pair_form_yields_pair_kind(tmp_path: Path) -> None:
    """Регрессия: прежняя форма читается как и раньше, вид выводится `pair`."""
    config = load_pipeline_config(
        _write(
            tmp_path,
            '[pipeline]\nspec_path = "docs/spec.md"\nplan_path = "docs/plan.md"\n'
            + _AGENTS,
        )
    )
    assert config.kind is PipelineKind.PAIR
    assert config.document_path is None


# --- fail-closed разбор формы (§3.2, C3) ------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '[pipeline]\ndocument_path = "d.md"\nspec_path = "s.md"\n',
        '[pipeline]\nspec_path = "s.md"\n',
        "[pipeline]\n",
        '[pipeline]\ndocument_path = "d.md"\nmax_architectural_returns = 2\n'
        + _DOC_CHECKLIST,
    ],
    ids=["mixed", "half-pair", "empty", "foreign-key"],
)
def test_every_form_refusal_names_both_schemas(tmp_path: Path, body: str) -> None:
    """C3 и §10: КАЖДЫЙ отказ формы перечисляет обе допустимые схемы.

    Проверять один лишь факт `raises` мало: диагностическая часть C3 —
    обязательное требование, и без утверждения о тексте она регрессирует
    молча, оставляя тест зелёным.
    """
    with pytest.raises(ConfigError) as excinfo:
        load_pipeline_config(_write(tmp_path, body + _AGENTS))
    message = str(excinfo.value)
    assert "document_path" in message
    assert "spec_path" in message
    assert "plan_path" in message


def test_foreign_key_refusal_also_names_the_key(tmp_path: Path) -> None:
    """Обе схемы — не вместо причины отказа, а вместе с ней."""
    body = (
        '[pipeline]\ndocument_path = "d.md"\nmax_architectural_returns = 2\n'
        + _DOC_CHECKLIST
    )
    with pytest.raises(ConfigError, match="max_architectural_returns"):
        load_pipeline_config(_write(tmp_path, body + _AGENTS))


# --- операторский чеклист контура `doc` (§5.3) ------------------------


def test_doc_checklist_order_follows_declaration(tmp_path: Path) -> None:
    """Порядок объявления, а не алфавит: промпт обязан быть воспроизводим."""
    path = _write(
        tmp_path,
        """
[pipeline]
document_path = "docs/charter.md"
[pipeline.checklists.doc]
findings_item = "B3"
[pipeline.checklists.doc.items]
B3 = "нет blocker/major-находок"
B1 = "каждый BEH-NN несёт traces:"
"""
        + _AGENTS,
    )
    checklist = load_pipeline_config(path).checklists["doc"]
    assert checklist.order == ("B3", "B1")
    assert checklist.findings_item == "B3"
    assert checklist.texts["B1"] == "каждый BEH-NN несёт traces:"


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            (
                "[pipeline.checklists.doc]\n"
                '[pipeline.checklists.doc.items]\nB1 = "нет находок"\n'
            ),
            "findings_item",
        ),
        (
            (
                '[pipeline.checklists.doc]\nfindings_item = "ZZ"\n'
                '[pipeline.checklists.doc.items]\nB1 = "x"\n'
            ),
            "ZZ",
        ),
        (
            (
                '[pipeline.checklists.doc]\nfindings_item = "B1"\n'
                "[pipeline.checklists.doc.items]\n"
            ),
            "пуст",
        ),
        ("", "обязательна"),
    ],
    ids=["no-findings-item", "findings-item-outside-items", "empty-items", "no-table"],
)
def test_doc_checklist_failures(tmp_path: Path, block: str, expected: str) -> None:
    """Вендоренного набора у операторского контура нет — все три отказа явные."""
    path = _write(
        tmp_path,
        '[pipeline]\ndocument_path = "docs/charter.md"\n' + block + _AGENTS,
    )
    with pytest.raises(ConfigError, match=expected):
        load_pipeline_config(path)


def test_pair_checklist_form_unchanged(tmp_path: Path) -> None:
    """Форма пары не менялась: плоское {id = текст}, состав фиксирован."""
    path = _write(
        tmp_path,
        """
[pipeline]
spec_path = "docs/spec.md"
plan_path = "docs/plan.md"
[pipeline.checklists.spec]
S1 = "переписанный текст"
"""
        + _AGENTS,
    )
    checklist = load_pipeline_config(path).checklists["spec"]
    assert checklist.order == ("S1", "S2", "S3", "S4", "S5")
    assert checklist.texts["S1"] == "переписанный текст"
    assert checklist.findings_item == "S1"


def test_document_kind_has_no_pair_checklists(tmp_path: Path) -> None:
    """Чеклисты чужого вида не конструируются вовсе (P10)."""
    config = load_pipeline_config(
        _write(
            tmp_path,
            '[pipeline]\ndocument_path = "docs/charter.md"\n'
            + _DOC_CHECKLIST
            + _AGENTS,
        )
    )
    assert set(config.checklists) == {"doc"}


# --- аксессоры путей по контуру (§5.1, §6) ----------------------------


def test_document_scope_allows_only_the_document(tmp_path: Path) -> None:
    config = load_pipeline_config(
        _write(
            tmp_path,
            '[pipeline]\ndocument_path = "docs/charter.md"\n'
            + _DOC_CHECKLIST
            + _AGENTS,
        )
    )
    assert config.scope_paths("doc") == ("docs/charter.md",)
    assert config.contour_documents("doc") == ("docs/charter.md",)
    assert config.documents() == ("docs/charter.md",)


def test_pair_accessors_keep_previous_boundaries(tmp_path: Path) -> None:
    """Регрессия §6: pair-контур ЧИТАЕТ спеку, но правит только план."""
    config = load_pipeline_config(
        _write(
            tmp_path,
            '[pipeline]\nspec_path = "docs/spec.md"\nplan_path = "docs/plan.md"\n'
            + _AGENTS,
        )
    )
    assert config.contour_documents("spec") == ("docs/spec.md",)
    assert config.contour_documents("pair") == ("docs/spec.md", "docs/plan.md")
    assert config.scope_paths("spec") == ("docs/spec.md",)
    assert config.scope_paths("pair") == ("docs/plan.md",)
    assert config.documents() == ("docs/spec.md", "docs/plan.md")


# --- BEH-18: формы взаимоисключающи, смена вида — drift (TASK-006) ----


def test_pipeline_forms_remain_exclusive_and_kind_change_is_drift(
    tmp_path: Path,
) -> None:
    """Консолидированная приёмка BEH-18 (WS-disputatio-65 TASK-006).

    Поведение доставлено ранее (эксклюзивность форм — схема закрытой
    секции, TASK-003 и PR #64; смена вида как drift — semantic comparison
    TASK-004, включая манифестную половину P0 из приёмки PR #93), поэтому
    задача закрыта tdd-waiver/v1 (spec/.tdd-evidence/waivers/…/TASK-006),
    а этот тест — её зелёная регрессия ровно по цели checklist'а: обе
    стороны исключения форм и drift смены вида В ОБОИХ направлениях —
    направление PAIR→DOCUMENT до него не проверял никто.
    """
    from disputatio.runtime.errors import SemanticDrift

    from ._pipeline_stand import Script, build_stand, start

    # Формы взаимоисключающи: смешанная секция и половина пары — отказ,
    # называющий обе схемы (C3).
    for body in (
        '[pipeline]\ndocument_path = "d.md"\nspec_path = "s.md"\n'
        'plan_path = "p.md"\n' + _DOC_CHECKLIST,
        '[pipeline]\nspec_path = "s.md"\n',
    ):
        with pytest.raises(ConfigError) as refusal:
            load_pipeline_config(_write(tmp_path, body + _AGENTS))
        assert "document_path" in str(refusal.value)
        assert "spec_path" in str(refusal.value)

    # DOCUMENT → PAIR: смена вида — drift, не миграция.
    doc_stand = build_stand(
        tmp_path / "doc",
        {"doc-r1": Script(outcome="park", raise_after_write=True)},
        kind=PipelineKind.DOCUMENT,
    )
    start(doc_stand)
    with pytest.raises(SemanticDrift) as doc_excinfo:
        doc_stand.resume_with(doc_stand.config_of_kind(PipelineKind.PAIR)).resume(
            "pair-docs"
        )
    assert "kind" in {diff.field for diff in doc_excinfo.value.diffs}

    # PAIR → DOCUMENT: симметрия, ранее не покрытая ни одним тестом.
    pair_stand = build_stand(
        tmp_path / "pair",
        {
            "spec-r1": Script(),
            "pair-r1": Script(outcome="park", issues=(), raise_after_write=True),
        },
        kind=PipelineKind.PAIR,
    )
    start(pair_stand)
    with pytest.raises(SemanticDrift) as pair_excinfo:
        pair_stand.resume_with(pair_stand.config_of_kind(PipelineKind.DOCUMENT)).resume(
            "pair-docs"
        )
    assert "kind" in {diff.field for diff in pair_excinfo.value.diffs}
