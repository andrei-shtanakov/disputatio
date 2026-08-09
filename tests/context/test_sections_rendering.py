"""Полнота полей в секциях `sections.py` — дополнение к `test_sections.py`.

Отдельный файл, а не правка `test_sections.py`: тот байт-залочен
red-чекпоинтом TASK-002. Мутационная проба показала, что основной набор
проверяет границы секций и правила отбора, но не полноту рендера: мутант,
выбрасывающий из issue `line_hint`/`suggestion` или из гейта `exit_code`/
`reason`, переживал весь suite. Автору и ревьюеру эти поля нужны, чтобы
попасть в нужное место файла и понять, почему гейт не запускался, — так что
их пропажа обязана валить тест здесь.
"""

import importlib
from types import ModuleType

from disputatio.contracts.review import Issue, Severity
from disputatio.contracts.verification import (
    DiffStats,
    GateResult,
    GateStatus,
    OverallStatus,
    VerificationReport,
)


def _load_sections() -> ModuleType:
    """Импортирует `disputatio.context.sections`; отсутствие — assertion."""
    try:
        return importlib.import_module("disputatio.context.sections")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.sections не импортируется: {exc}"
        ) from exc


def test_render_issues_section_shows_every_filled_field() -> None:
    """Все заполненные поля issue доходят до промпта дословно."""
    sections = _load_sections()
    issue = Issue(
        id="R3-2",
        severity=Severity.MAJOR,
        file="src/disputatio/context/author.py",
        line_hint=42,
        claim="секция директивы рендерится пустой",
        evidence="tests/context/test_sections.py::test_render_sections_are_empty",
        suggestion='вернуть "" вместо заголовка',
    )

    text = sections.render_issues_section([issue], title=sections.OPEN_ISSUES_TITLE)

    for expected in (
        "R3-2",
        "major",
        "src/disputatio/context/author.py",
        "42",
        issue.claim,
        issue.evidence,
        issue.suggestion,
    ):
        assert expected in text


def test_render_issues_section_omits_unset_optional_fields() -> None:
    """Незаданные `line_hint`/`suggestion`/`evidence` не порождают пустых строк."""
    sections = _load_sections()
    issue = Issue(
        id="R3-1",
        severity=Severity.NIT,
        file="README.md",
        claim="опечатка в заголовке",
    )

    text = sections.render_issues_section([issue], title=sections.RESOLVED_ISSUES_TITLE)

    assert "line_hint" not in text
    assert "suggestion" not in text
    assert "evidence" not in text
    assert "опечатка в заголовке" in text


def test_render_gate_blocks_show_exit_code_and_reason() -> None:
    """`exit_code` провалившегося и `reason` пропущенного гейта доходят до промпта."""
    sections = _load_sections()
    report = VerificationReport(
        round=1,
        gates=[
            GateResult(
                name="pytest",
                cmd="uv run pytest -q",
                status=GateStatus.FAIL,
                exit_code=17,
                tail="1 failed",
            ),
            GateResult(
                name="typecheck",
                cmd="pyrefly check",
                status=GateStatus.SKIP,
                reason="pyrefly не сконфигурирован",
            ),
        ],
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=0, insertions=0, deletions=0),
    )

    text = sections.render_verification_section(report)

    assert "17" in text
    assert "pyrefly не сконфигурирован" in text
    # skip-гейт не выдумывает exit_code, которого в артефакте нет.
    assert text.count("exit_code") == 1


def test_unknown_severity_sorts_last_without_raising() -> None:
    """Неизвестная severity не роняет отбор — уезжает в конец списка."""
    sections = _load_sections()
    known = Issue(id="A", severity=Severity.NIT, file="a.py", claim="c")
    unknown = known.model_copy(update={"id": "B", "severity": "cosmic"})

    selected = sections.select_open_issues([unknown, known], ["A", "B"])

    assert [issue.id for issue in selected] == ["A", "B"]
