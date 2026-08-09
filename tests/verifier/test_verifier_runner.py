"""`VerifierRunner.verify()` — реализация порта (TASK-008, [DESIGN-003]).

Покрывает связку REQ-001 (порт + shape отчёта), REQ-002 (порядок и
идентичность gates, пустой список) и REQ-008 (агрегат `overall`) на
уровне `verify()`, а не составных частей: `run_gate`, `collect_diff_stats`
и `compute_overall` уже покрыты своими задачами, здесь проверяется, что
они действительно СОБРАНЫ вместе и результаты не подменены константами.

Импорт `disputatio.verifier.runner_impl` ленив: на момент red-чекпоинта
модуля ещё нет, и импорт на уровне модуля сломал бы collection всего
каталога. Хелпер пересобирает `ImportError` в `AssertionError` — гейт
принимает red только при падении assertion'ом.
"""

import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from disputatio.contracts.base import SCHEMA_V1
from disputatio.contracts.ports import Verifier
from disputatio.contracts.verification import GateStatus, OverallStatus

if TYPE_CHECKING:
    from disputatio.verifier import GateSpec


def _runner_class() -> Any:
    """Возвращает `VerifierRunner` из пакета; отсутствие — `AssertionError`."""
    try:
        from disputatio.verifier import VerifierRunner
    except ImportError as exc:  # red-фаза: runner_impl.py ещё не создан
        raise AssertionError(
            "disputatio.verifier не экспортирует VerifierRunner"
        ) from exc
    return VerifierRunner


def _python_cmd(code: str) -> str:
    """Собирает команду `<интерпретатор> -c <code>` с корректным quoting'ом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _printing_cmd(count: int) -> str:
    """Команда, печатающая строки `line-0`…`line-{count-1}` и выходящая с 0."""
    return _python_cmd(f"[print(f'line-{{i}}') for i in range({count})]")


def test_verifier_runner_satisfies_verifier_port(tmp_git_repo: Path) -> None:
    """[REQ-001]: экземпляр удовлетворяет Protocol `Verifier`."""
    runner_class = _runner_class()

    runner = runner_class([], tmp_git_repo)

    assert isinstance(runner, Verifier)


def test_gates_keep_config_order_and_identity(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """[REQ-002]: N gates → N результатов в порядке конфигурации, с их name/cmd.

    Порядок специально не алфавитный и не совпадает с порядком статусов:
    реализация, сортирующая gates (или группирующая упавшие), провалит
    сравнение списков, а не только их длину.
    """
    runner_class = _runner_class()
    specs = [
        make_gate_spec(name="zeta", cmd=_python_cmd("raise SystemExit(0)")),
        make_gate_spec(name="alpha", cmd=_python_cmd("raise SystemExit(3)")),
        make_gate_spec(name="mid", cmd="disputatio-never-run", enabled=False),
    ]

    report = runner_class(specs, tmp_git_repo).verify(1)

    assert [gate.name for gate in report.gates] == ["zeta", "alpha", "mid"]
    assert [gate.cmd for gate in report.gates] == [spec.cmd for spec in specs]
    assert [gate.status for gate in report.gates] == [
        GateStatus.PASS,
        GateStatus.FAIL,
        GateStatus.SKIP,
    ]
    # Исход упавшего gate доходит до отчёта целиком, а не только статусом:
    # `verify()`, пересобирающий `GateResult` из одного статуса, потерял бы код.
    assert report.gates[1].exit_code == 3


def test_empty_gate_list_gives_no_gates_and_overall_pass(tmp_git_repo: Path) -> None:
    """[REQ-002]/[REQ-008]: analyze-режим — пустой список валиден, `overall == pass`."""
    runner_class = _runner_class()

    report = runner_class([], tmp_git_repo).verify(1)

    assert report.gates == []
    assert report.overall == OverallStatus.PASS


def test_report_carries_round_and_schema(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """[REQ-001]: `round == round_no`, схема — `disputatio/v1`.

    `round_no` берётся именно из аргумента, а не из длины списка gates и
    не из счётчика вызовов: раунд 7 при одном gate ловит обе подмены.
    """
    runner_class = _runner_class()
    specs = [make_gate_spec(name="tests", cmd=_python_cmd("raise SystemExit(0)"))]

    report = runner_class(specs, tmp_git_repo).verify(7)

    assert report.round == 7
    assert report.schema_ == SCHEMA_V1
    assert report.model_dump()["schema"] == "disputatio/v1"


def test_single_failing_gate_makes_overall_fail(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """[REQ-008]: один `fail` среди pass/skip валит `overall`.

    Парная проверка pass-набора — против реализации, зашившей `fail`
    константой: без неё тест прошёл бы и на всегда-красном агрегате.
    """
    runner_class = _runner_class()
    green = make_gate_spec(name="tests", cmd=_python_cmd("raise SystemExit(0)"))
    skipped = make_gate_spec(name="types", cmd="disputatio-never-run", enabled=False)
    red = make_gate_spec(name="lint", cmd=_python_cmd("raise SystemExit(1)"))

    healthy = runner_class([green, skipped], tmp_git_repo).verify(1)
    broken = runner_class([green, skipped, red], tmp_git_repo).verify(1)

    assert healthy.overall == OverallStatus.PASS
    assert broken.overall == OverallStatus.FAIL


def test_diff_stats_reflect_working_tree(tmp_git_repo: Path) -> None:
    """[REQ-007] в связке: `diff_stats` снимается с дерева, а не занулён.

    Без изменений в дереве — нули; после правки tracked-файла — один файл
    с непустой статистикой. Реализация, подставляющая `DiffStats(0, 0, 0)`
    вместо вызова `collect_diff_stats`, провалит вторую половину.
    """
    runner_class = _runner_class()

    clean = runner_class([], tmp_git_repo).verify(1)
    (tmp_git_repo / "tracked.txt").write_text("mutated\nsecond\n", encoding="utf-8")
    dirty = runner_class([], tmp_git_repo).verify(1)

    assert (clean.diff_stats.files, clean.diff_stats.insertions) == (0, 0)
    assert dirty.diff_stats.files == 1
    assert dirty.diff_stats.insertions == 2
    assert dirty.diff_stats.deletions == 1


def test_tail_lines_reaches_gate_execution(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """[REQ-005]: `tail_lines` конструктора доходит до запуска gate.

    Параметр объявлен интерфейсом [DESIGN-003]; принятый и проигнорированный,
    он был бы ложью в API — 10 напечатанных строк при `tail_lines=2` дают
    ровно две последние.
    """
    runner_class = _runner_class()
    specs = [make_gate_spec(name="noisy", cmd=_printing_cmd(10))]

    report = runner_class(specs, tmp_git_repo, tail_lines=2).verify(1)

    assert report.gates[0].tail == "line-8\nline-9"


def test_verify_is_independent_between_rounds(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """[DESIGN-003]: повторный `verify()` не копит состояние между раундами.

    Один и тот же экземпляр вызывается дважды: реализация, накапливающая
    результаты в поле, вернула бы во втором отчёте четыре gate вместо двух.
    """
    runner_class = _runner_class()
    specs = [
        make_gate_spec(name="tests", cmd=_python_cmd("raise SystemExit(0)")),
        make_gate_spec(name="lint", cmd=_python_cmd("raise SystemExit(1)")),
    ]
    runner = runner_class(specs, tmp_git_repo)

    first = runner.verify(1)
    second = runner.verify(2)

    assert [gate.name for gate in second.gates] == ["tests", "lint"]
    assert (first.round, second.round) == (1, 2)
    assert first.overall == second.overall == OverallStatus.FAIL
