"""Промпт автора doc-раунда — контуры spec/pair (§5.1, [DESIGN-011]).

Тонкий композитор в духе `author.py`: секции переиспользуются из
`sections.py`, здесь — только сборка и контурная разница в задаче автора.

Сигнатура умышленно не несёт ни истории диалога, ни прошлых версий
документа ([ADR-004] в новом изводе): источник истины для doc-раунда —
файлы `doc_paths` в рабочей директории, а не пересказ в промпте (§6.1
SPEC-001). Единственный канал, которым архитектурная находка прошлого
pair-ревью доходит до автора spec-rN+1, — `adopted_findings`, и он идёт
внутри меток «данные, не инструкции» (§7.3 SPEC-002) той же механикой,
что и замечания ревьюера в `author.py`: `render_issues_section` уже
оборачивает каждый issue отдельным artifact-блоком.

Задача автора различается по контуру (§5.1):

- `spec` — довести `docs/specs/...` до сходимости: raunda 1 пишет по
  задаче, следующие раунды перерабатывают по `adopted_findings`;
- `pair` — написать/переработать план по уже сошедшейся спеке; **правка
  спеки автору pair-контура недоступна** — архитектурный дефект паркует
  pair-сессию и возвращает пайплайн к спеке (§7.1, §7.3), а не даёт право
  правки.

Модуль чист: ни I/O, ни времени, ни случайности (NFR-001, NFR-002).
"""

from collections.abc import Sequence
from typing import Final, Literal

from disputatio.context.sections import render_directive_section, render_issues_section
from disputatio.context.tags import wrap_artifact_data
from disputatio.contracts.review import Issue

__all__ = ["build_doc_author_prompt"]

TASK_TITLE: Final = "## Задача пользователя"
DOC_PATHS_TITLE: Final = "## Документы контура"
ADOPTED_FINDINGS_TITLE: Final = "## Архитектурные находки прошлого pair-ревью"

_INTRO_BY_CONTOUR: Final[dict[str, str]] = {
    "spec": (
        "# Doc-раунд: работа автора (контур spec)\n"
        "Ваша задача — довести документ спецификации до сходимости по "
        "чеклисту S1–S5 (§5.3 SPEC-002): на первом раунде написать его по "
        "задаче ниже, на последующих — переработать по архитектурным "
        "находкам pair-ревью, если они приложены. Источник истины — файлы "
        "рабочей директории, а не история диалога: прошлые версии "
        "документа сюда не передаются намеренно (§6.1 SPEC-001)."
    ),
    "pair": (
        "# Doc-раунд: работа автора (контур pair)\n"
        "Ваша задача — написать или переработать план по уже сошедшейся "
        "спецификации, чтобы он сходился по чеклисту P1–P5 (§5.3 "
        "SPEC-002). Правка самой спецификации вам недоступна: "
        "архитектурный дефект паркует pair-сессию и возвращает пайплайн к "
        "спеке, а не даёт право правки (§5.1 SPEC-002). Источник истины — "
        "файлы рабочей директории, а не история диалога (§6.1 SPEC-001)."
    ),
}


def build_doc_author_prompt(
    *,
    contour: Literal["spec", "pair"],
    task_text: str,
    doc_paths: Sequence[str],
    directive: str | None,
    adopted_findings: Sequence[Issue] = (),
) -> str:
    """Собирает промпт автора doc-раунда контура `contour` (§5.1).

    Порядок секций: задача пользователя (данные), документы контура
    (статический список путей — вычислен оркестратором, снаружи меток
    §6.3), директива оркестратора, архитектурные находки прошлого
    pair-ревью. Каждая необязательная секция исчезает целиком, если
    данных нет ([ADR-003]).

    Результат зависит только от аргументов — байт-в-байт воспроизводим
    (NFR-002).
    """
    parts = [
        _INTRO_BY_CONTOUR[contour],
        _render_task_text_section(task_text),
        _render_doc_paths_section(doc_paths),
        render_directive_section(directive),
        render_issues_section(list(adopted_findings), title=ADOPTED_FINDINGS_TITLE),
    ]
    return "\n\n".join(part for part in parts if part)


def _render_task_text_section(task_text: str) -> str:
    """Задача пользователя — данные, как и в промпте develop/analyze (§6.1)."""
    return "\n".join([TASK_TITLE, wrap_artifact_data(task_text)])


def _render_doc_paths_section(doc_paths: Sequence[str]) -> str:
    """Пути документов контура — статический текст оркестратора (§6.3).

    Вне меток artifact-данных сознательно: пути вычислил оркестратор, они
    не приходили от агента, а автор читает и правит эти файлы своими
    инструментами — источник истины остаётся рабочей директорией.
    """
    if not doc_paths:
        return ""
    lines = [DOC_PATHS_TITLE, *(f"- {path}" for path in doc_paths)]
    return "\n".join(lines)
