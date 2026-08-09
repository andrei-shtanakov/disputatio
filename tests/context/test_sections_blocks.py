"""Статус гейта и гранулярность artifact-блоков — ревью-дополнение к TASK-002.

Отдельный файл: `test_sections.py` байт-залочен red-чекпоинтом, а
`test_sections_rendering.py` закрывает полноту опциональных полей. Здесь
пинятся два инварианта `sections.py`, которые мутационная проба нашла
непокрытыми — мутанты переживали весь suite:

* `status`/`cmd` гейта. Секция §6.2 существует ровно ради того, чтобы
  ревьюер отличал «проверено и прошло» от «не запускалось»; без строки
  статуса блоки гейтов неразличимы, а без `cmd` автор не воспроизведёт
  провал. Оба поля выпадали молча.
* Один artifact-блок на issue и на гейт (инвариант 2 в докстринге модуля).
  Обёртка «одна на всю секцию» проходила старые тесты: они подавали ровно
  один элемент, и баланс меток сходился.
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


def _load_tags() -> ModuleType:
    """Импортирует `disputatio.context.tags`; отсутствие — assertion."""
    try:
        return importlib.import_module("disputatio.context.tags")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.tags не импортируется: {exc}"
        ) from exc


def _passed_gate() -> GateResult:
    """Прошедший гейт `ruff`."""
    return GateResult(
        name="ruff", cmd="ruff check .", status=GateStatus.PASS, exit_code=0
    )


def _failed_gate(name: str = "pytest", cmd: str = "uv run pytest -q") -> GateResult:
    """Провалившийся гейт с непустым `tail`."""
    return GateResult(
        name=name, cmd=cmd, status=GateStatus.FAIL, exit_code=1, tail="1 failed"
    )


def _skipped_gate() -> GateResult:
    """Пропущенный гейт `typecheck` с `reason` (§4.3)."""
    return GateResult(
        name="typecheck",
        cmd="pyrefly check",
        status=GateStatus.SKIP,
        reason="pyrefly не сконфигурирован",
    )


def _report(*gates: GateResult) -> VerificationReport:
    """Отчёт проверок с переданными гейтами."""
    return VerificationReport(
        round=1,
        gates=list(gates),
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=1, insertions=2, deletions=3),
    )


def test_verification_section_states_status_of_every_gate() -> None:
    """У каждого гейта в §6.2 своя строка статуса: pass/fail/skip различимы."""
    sections = _load_sections()

    lines = sections.render_verification_section(
        _report(_passed_gate(), _failed_gate(), _skipped_gate())
    ).splitlines()

    assert "status: pass" in lines
    assert "status: fail" in lines
    assert "status: skip" in lines


def test_failed_gates_section_states_status_and_command() -> None:
    """Провалившийся гейт несёт и статус, и команду — автору нужно воспроизвести."""
    sections = _load_sections()

    lines = sections.render_failed_gates_section(
        [_failed_gate(name="mypy", cmd="uv run mypy src")]
    ).splitlines()

    assert "status: fail" in lines
    assert "cmd: uv run mypy src" in lines


def test_verification_section_states_command_of_every_gate() -> None:
    """Команда каждого гейта доходит до промпта дословно, включая skip."""
    sections = _load_sections()

    lines = sections.render_verification_section(
        _report(_passed_gate(), _failed_gate(), _skipped_gate())
    ).splitlines()

    assert "cmd: ruff check ." in lines
    assert "cmd: uv run pytest -q" in lines
    assert "cmd: pyrefly check" in lines


def test_each_issue_gets_its_own_artifact_block() -> None:
    """Метки ставятся на каждый issue, а не одни на всю секцию."""
    sections = _load_sections()
    tags = _load_tags()
    issues = [
        Issue(id="R2-1", severity=Severity.BLOCKER, file="a.py", claim="раз"),
        Issue(id="R2-2", severity=Severity.MAJOR, file="b.py", claim="два"),
        Issue(id="R2-3", severity=Severity.NIT, file="c.py", claim="три"),
    ]

    text = sections.render_issues_section(issues, title=sections.OPEN_ISSUES_TITLE)

    assert text.count(tags._OPEN_TAG) == 3
    assert text.count(tags._CLOSE_TAG) == 3


def test_each_gate_gets_its_own_artifact_block() -> None:
    """То же для гейтов — и в секции проваленных, и в полном отчёте."""
    sections = _load_sections()
    tags = _load_tags()
    failed = [_failed_gate(), _failed_gate(name="mypy", cmd="uv run mypy src")]

    gates_text = sections.render_failed_gates_section([*failed, _passed_gate()])
    report_text = sections.render_verification_section(
        _report(_passed_gate(), _failed_gate(), _skipped_gate())
    )

    assert gates_text.count(tags._OPEN_TAG) == 2
    assert gates_text.count(tags._CLOSE_TAG) == 2
    assert report_text.count(tags._OPEN_TAG) == 3
    assert report_text.count(tags._CLOSE_TAG) == 3
