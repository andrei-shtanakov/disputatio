"""Промпты контура `doc` — вида пайплайна `document`: SPEC-002 §5.1, §5.2.

Контур один, возвращаться некуда, чеклист объявляет оператор. Отсюда три
отличия от встроенных контуров, и каждое проверяется здесь:

* интро автора говорит про ЕДИНСТВЕННЫЙ документ и не обещает ни пары, ни
  архитектурных находок прошлого pair-ревью — их у вида не бывает;
* промпт ревьюера не несёт требования `defect_class`: маршрута возврата нет,
  и просить класс дефекта значило бы просить заполнить поле, которое никто
  не читает (V5 §5.2, P10);
* порядок пунктов чеклиста — объявленный оператором, а не алфавитный:
  он часть чеклиста (§5.3), и промпт обязан быть байт-в-байт воспроизводим.
"""

from disputatio.context.doc_author import build_doc_author_prompt
from disputatio.context.doc_reviewer import build_doc_reviewer_prompt
from disputatio.contracts.checklists_catalog import ResolvedChecklist
from disputatio.contracts.verification import VerificationReport

_DOC_CHECKLIST = ResolvedChecklist(
    order=("B3", "B1"),
    texts={"B3": "третий по имени, первый по объявлению", "B1": "первый по имени"},
    findings_item="B3",
)


def _verification() -> VerificationReport:
    return VerificationReport.model_validate(
        {
            "schema": "disputatio/v1",
            "round": 1,
            "gates": [],
            "overall": "pass",
            "diff_stats": {"files": 1, "insertions": 2, "deletions": 0},
        }
    )


# --- автор контура `doc` (§5.1) ---------------------------------------


def test_doc_author_prompt_has_own_intro() -> None:
    """У контура своё интро: единственный документ, и правит он только его."""
    prompt = build_doc_author_prompt(
        contour="doc",
        task_text="написать чартер",
        doc_paths=("docs/charter.md",),
        directive=None,
    )
    assert "docs/charter.md" in prompt
    intro = prompt.split("## Задача пользователя")[0]
    assert "контур doc" in intro
    assert "doc-scope" in intro


def test_doc_author_intro_promises_no_pair_machinery() -> None:
    """Ни пары, ни возврата к спеке — этой механики у вида нет (P10)."""
    intro = build_doc_author_prompt(
        contour="doc",
        task_text="написать чартер",
        doc_paths=("docs/charter.md",),
        directive=None,
    ).split("## Задача пользователя")[0]
    assert "pair" not in intro
    assert "спек" not in intro.lower()


def test_doc_author_prompt_is_byte_reproducible() -> None:
    args = {
        "contour": "doc",
        "task_text": "написать чартер",
        "doc_paths": ("docs/charter.md",),
        "directive": "уточнить BEH-02",
    }
    assert build_doc_author_prompt(**args) == build_doc_author_prompt(**args)


# --- ревьюер контура `doc` (§5.2) -------------------------------------


def test_doc_reviewer_prompt_orders_by_resolved_checklist() -> None:
    """Порядок — объявленный оператором, а не алфавитный (§5.3)."""
    prompt = build_doc_reviewer_prompt(
        contour="doc",
        doc_texts={"docs/charter.md": "# Ч"},
        verification=_verification(),
        checklist=_DOC_CHECKLIST,
    )
    assert prompt.index("- B3:") < prompt.index("- B1:")


def test_doc_reviewer_prompt_has_no_defect_class_note() -> None:
    """Возвращаться некуда — требование класса дефекта было бы ложью."""
    prompt = build_doc_reviewer_prompt(
        contour="doc",
        doc_texts={"docs/charter.md": "# Ч"},
        verification=_verification(),
        checklist=_DOC_CHECKLIST,
    )
    assert "defect_class" not in prompt


def test_doc_reviewer_prompt_carries_operator_texts_verbatim() -> None:
    """Единственный канал операторского критерия до ревьюера — этот параметр."""
    prompt = build_doc_reviewer_prompt(
        contour="doc",
        doc_texts={"docs/charter.md": "# Ч"},
        verification=_verification(),
        checklist=_DOC_CHECKLIST,
    )
    for text in _DOC_CHECKLIST.texts.values():
        assert text in prompt


def test_doc_reviewer_prompt_has_own_intro() -> None:
    prompt = build_doc_reviewer_prompt(
        contour="doc",
        doc_texts={"docs/charter.md": "# Ч"},
        verification=_verification(),
        checklist=_DOC_CHECKLIST,
    )
    intro = prompt.split("## Документы контура")[0]
    assert "контур doc" in intro
    assert "S1" not in intro and "P1" not in intro


def test_reviewer_prompt_is_byte_reproducible() -> None:
    args = {
        "contour": "doc",
        "doc_texts": {"docs/charter.md": "# Ч"},
        "verification": _verification(),
        "checklist": _DOC_CHECKLIST,
    }
    assert build_doc_reviewer_prompt(**args) == build_doc_reviewer_prompt(**args)
