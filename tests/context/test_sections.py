"""Тесты `sections.py` — отбор, сортировка и рендер секций: TASK-002, [DESIGN-003].

Модуль загружается лениво (`_load_sections`), как и в `test_tags.py`: на
red-чекпоинте `disputatio.context.sections` ещё не существует, а импорт на
уровне модуля сорвал бы collection всего каталога — гейт увидел бы `error`
вместо честного assertion'а.

Что здесь действительно пинится:

* правила отбора — фильтр по `open_issue_ids`, порядок severity и его
  стабильность, «только `fail`» для гейтов (сравнение значением, не
  идентичностью: `model_copy` возвращает сырую строку);
* правило ADR-003 «пустая секция не рендерится вообще» — `""`, а не
  заголовок с «(нет)»;
* граница artifact-меток: заголовок секции — снаружи, артефактный текст —
  внутри. Тексты меток берутся из `tags`, а не копируются сюда: их
  литеральное содержимое — деталь реализации [ADR-001].
"""

import importlib
import inspect
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


def _load_tags() -> ModuleType:
    """Импортирует `disputatio.context.tags`; отсутствие — assertion."""
    try:
        return importlib.import_module("disputatio.context.tags")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.tags не импортируется: {exc}"
        ) from exc


def _issue(
    issue_id: str,
    severity: Severity,
    *,
    claim: str = "претензия",
    evidence: str = "улика",
    file: str = "src/x.py",
    line_hint: int | None = None,
    suggestion: str | None = None,
) -> Issue:
    """Issue с осмысленными дефолтами — в тестах задаётся только значимое."""
    return Issue(
        id=issue_id,
        severity=severity,
        file=file,
        line_hint=line_hint,
        claim=claim,
        evidence=evidence,
        suggestion=suggestion,
    )


def _failed_gate(tail: str = "FAILED tests/test_x.py::test_y\n1 failed") -> GateResult:
    """Провалившийся гейт `pytest` с непустым `tail`."""
    return GateResult(
        name="pytest",
        cmd="uv run pytest -q",
        status=GateStatus.FAIL,
        exit_code=1,
        tail=tail,
    )


def _passed_gate() -> GateResult:
    """Прошедший гейт `ruff` — в секцию проваленных попадать не должен."""
    return GateResult(
        name="ruff", cmd="ruff check .", status=GateStatus.PASS, exit_code=0
    )


def _skipped_gate() -> GateResult:
    """Пропущенный гейт `typecheck` с непустым `reason` (§4.3)."""
    return GateResult(
        name="typecheck",
        cmd="pyrefly check",
        status=GateStatus.SKIP,
        reason="not configured",
    )


def test_select_open_issues_filters_and_sorts_by_severity() -> None:
    """Остаются только id из `open_issue_ids`, порядок blocker→major→minor→nit."""
    sections = _load_sections()
    issues = [
        _issue("I-nit", Severity.NIT),
        _issue("I-blocker", Severity.BLOCKER),
        _issue("I-closed", Severity.MAJOR),
        _issue("I-minor", Severity.MINOR),
        _issue("I-major", Severity.MAJOR),
    ]

    selected = sections.select_open_issues(
        issues, ["I-nit", "I-blocker", "I-minor", "I-major"]
    )

    assert [issue.id for issue in selected] == [
        "I-blocker",
        "I-major",
        "I-minor",
        "I-nit",
    ]


def test_select_open_issues_sort_is_stable_within_severity() -> None:
    """Внутри одной severity сохраняется исходный относительный порядок."""
    sections = _load_sections()
    issues = [
        _issue("z-1", Severity.MAJOR),
        _issue("a-2", Severity.MAJOR),
        _issue("m-3", Severity.BLOCKER),
        _issue("b-4", Severity.BLOCKER),
    ]

    selected = sections.select_open_issues(issues, [issue.id for issue in issues])

    # Ни сортировки по id, ни reverse по вторичному ключу: порядок внутри
    # severity — ровно тот, в каком issues пришли от ревьюера.
    assert [issue.id for issue in selected] == ["m-3", "b-4", "z-1", "a-2"]


def test_select_resolved_issues_is_exact_complement() -> None:
    """Решённые — ровно те, чьих id НЕТ в `open_issue_ids`; без пересечений."""
    sections = _load_sections()
    issues = [
        _issue("R2-1", Severity.MINOR),
        _issue("R2-2", Severity.BLOCKER),
        _issue("R2-3", Severity.MAJOR),
    ]
    open_ids = ["R2-2"]

    resolved = sections.select_resolved_issues(issues, open_ids)
    still_open = sections.select_open_issues(issues, open_ids)

    assert [issue.id for issue in resolved] == ["R2-3", "R2-1"]
    resolved_ids = {issue.id for issue in resolved}
    open_selected_ids = {issue.id for issue in still_open}
    assert resolved_ids & open_selected_ids == set()
    assert resolved_ids | open_selected_ids == {issue.id for issue in issues}


def test_select_failed_gates_keeps_only_failures() -> None:
    """`pass`/`skip` отсеиваются; сравнение по значению, а не по identity."""
    sections = _load_sections()
    passed = _passed_gate()
    raw_failed = passed.model_copy(update={"name": "mypy", "status": "fail"})
    # model_copy обходит валидацию: статус остаётся сырой строкой. Реализация
    # на `is` тихо потеряла бы этот гейт — фиксируем разницу явно.
    assert raw_failed.status == GateStatus.FAIL
    assert raw_failed.status is not GateStatus.FAIL

    selected = sections.select_failed_gates(
        [passed, _failed_gate(), _skipped_gate(), raw_failed]
    )

    assert [gate.name for gate in selected] == ["pytest", "mypy"]


def test_render_sections_are_empty_on_empty_input() -> None:
    """Пустой вход — пустая строка: ни заголовка, ни «(нет)» (ADR-003)."""
    sections = _load_sections()

    assert sections.render_issues_section([], title="## Заголовок") == ""
    assert sections.render_failed_gates_section([]) == ""
    assert sections.render_failed_gates_section([_passed_gate()]) == ""
    assert sections.render_directive_section(None) == ""
    assert sections.render_directive_section("") == ""
    assert sections.render_directive_section("   \n") == ""
    assert sections.render_verification_section(None) == ""


def test_render_failed_gates_section_shows_name_and_tail() -> None:
    """Печатаются имя и `tail` провалившегося гейта; прошедший не упомянут."""
    sections = _load_sections()
    tail = "FAILED tests/test_parser.py::test_roundtrip\n1 failed, 12 passed"

    text = sections.render_failed_gates_section([_failed_gate(tail), _passed_gate()])

    assert "pytest" in text
    assert tail in text
    assert "ruff" not in text
    # Никакого источника вывода сверх `tail`: у функции ровно один параметр —
    # список гейтов, полного stdout ей неоткуда взять.
    assert list(inspect.signature(sections.render_failed_gates_section).parameters) == [
        "gates"
    ]


def test_render_verification_section_includes_every_gate_and_summary() -> None:
    """Все гейты независимо от статуса + `reason` у skip, `overall`, diff_stats."""
    sections = _load_sections()
    tail = "FAILED tests/test_parser.py::test_roundtrip"
    report = VerificationReport(
        round=2,
        gates=[_passed_gate(), _failed_gate(tail), _skipped_gate()],
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=7, insertions=123, deletions=45),
    )

    text = sections.render_verification_section(report)

    for name in ("ruff", "pytest", "typecheck"):
        assert name in text
    assert "not configured" in text
    assert tail in text
    assert "overall: fail" in text.splitlines()
    stats_lines = [line for line in text.splitlines() if line.startswith("diff_stats")]
    assert len(stats_lines) == 1
    assert "7" in stats_lines[0]
    assert "123" in stats_lines[0]
    assert "45" in stats_lines[0]


def test_non_empty_sections_wrap_artifact_text_with_title_outside() -> None:
    """Заголовок секции — вне меток, артефактный текст — между ними."""
    sections = _load_sections()
    tags = _load_tags()
    open_tag = tags._OPEN_TAG
    close_tag = tags._CLOSE_TAG
    title = "## Открытые замечания"
    issue = _issue("R2-1", Severity.BLOCKER, claim="сломан парсер фронтматтера")
    report = VerificationReport(
        round=2,
        gates=[_failed_gate()],
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=1, insertions=2, deletions=3),
    )
    rendered = {
        "issues": sections.render_issues_section([issue], title=title),
        "gates": sections.render_failed_gates_section([_failed_gate()]),
        "directive": sections.render_directive_section("почини парсер"),
        "verification": sections.render_verification_section(report),
    }

    for name, text in rendered.items():
        assert text.count(open_tag) == text.count(close_tag) >= 1, name
        assert text.index(open_tag) < text.index(close_tag), name
        # Всё, что стоит до первой метки, — статический текст оркестратора.
        assert text[: text.index(open_tag)].strip(), name

    issues_text = rendered["issues"]
    assert issues_text.startswith(title)
    assert title not in issues_text[issues_text.index(open_tag) :]
    claim_at = issues_text.index("сломан парсер фронтматтера")
    assert issues_text.index(open_tag) < claim_at < issues_text.index(close_tag)

    directive_text = rendered["directive"]
    directive_at = directive_text.index("почини парсер")
    assert directive_text.index(open_tag) < directive_at
    assert directive_at < directive_text.index(close_tag)

    verification_text = rendered["verification"]
    assert verification_text.index("overall: fail") < verification_text.index(open_tag)
