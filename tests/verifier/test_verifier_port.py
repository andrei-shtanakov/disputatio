"""Приёмка workstream'а Verifier (TASK-011, §6 requirements).

Три проверки, отвечающие трём критериям приёмки:

1. e2e-прогон публичного порта на реалистичном наборе gates (pass + fail +
   два вида skip) с полной проверкой формы `VerificationReport` — критерий
   §6.3 «пригодна для composition root без правок contracts».
2. Матрица [REQ-001]…[REQ-011] → покрывающие тесты: каждое требование
   названо вместе с файлом и именем теста, и существование теста
   проверяется разбором AST — критерий §6.1. Матрица ломается на
   переименовании или удалении покрывающего теста, поэтому traceability
   остаётся живой, а не прозой в PR.
3. Полнота evidence workstream'а (claims/verdicts) — NFR-002: TDD-цикл
   каждой задачи действительно зафиксирован на диске.

Пакет импортируется только через публичный API `disputatio.verifier`
([DESIGN-001]): приёмка смотрит на Verifier глазами w-runtime, а не через
подмодули. Импорт ленив по той же причине, что и в остальных тестах
каталога — на red-фазе модуля может не быть, и падение обязано быть
`AssertionError`, а не ошибкой сборки коллекции.
"""

import ast
import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from disputatio.contracts.base import SCHEMA_V1
from disputatio.contracts.ports import Verifier
from disputatio.contracts.verification import (
    GateStatus,
    OverallStatus,
    VerificationReport,
)

if TYPE_CHECKING:
    from disputatio.verifier import GateSpec

_TESTS_DIR: Final = Path(__file__).resolve().parent
_REPO_ROOT: Final = _TESTS_DIR.parents[1]

_NAMESPACE: Final = "ws-w-verifier"

# Задачи workstream'а, чей TDD-цикл уже закрыт вердиктом на момент
# приёмки (TASK-011 — текущая, её вердикт пишется ПОСЛЕ этого теста).
_CLOSED_TASKS: Final = tuple(f"TASK-{n:03d}" for n in range(1, 11))
_ACCEPTANCE_TASK: Final = "TASK-011"

# [REQ-NNN] → покрывающие тесты как пары (модуль, имя теста). Пары, а не
# node-id-строки: implicit-concat в длинном node-id ловится ruff'ом (ISC004),
# а пара ещё и разбирается без парсинга разделителя.
_REQUIREMENT_COVERAGE: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "REQ-001": (
        ("test_verifier_runner.py", "test_verifier_runner_satisfies_verifier_port"),
        ("test_verifier_runner.py", "test_report_carries_round_and_schema"),
        ("test_verifier_port.py", "test_realistic_gate_mix_produces_a_full_report"),
    ),
    "REQ-002": (
        ("test_verifier_runner.py", "test_gates_keep_config_order_and_identity"),
        (
            "test_verifier_runner.py",
            "test_empty_gate_list_gives_no_gates_and_overall_pass",
        ),
    ),
    "REQ-003": (
        ("test_capture.py", "test_exit_code_is_the_checked_process_code"),
        (
            "test_capture.py",
            "test_declared_shell_pipeline_keeps_its_own_exit_semantics",
        ),
    ),
    "REQ-004": (
        ("test_gate_execution.py", "test_zero_exit_code_maps_to_pass"),
        ("test_gate_execution.py", "test_nonzero_exit_code_maps_to_fail"),
        (
            "test_gate_execution.py",
            "test_disabled_gate_is_skipped_without_starting_a_process",
        ),
        ("test_verifier_port.py", "test_realistic_gate_mix_produces_a_full_report"),
    ),
    "REQ-005": (
        (
            "test_tail_bounding.py",
            "test_tail_keeps_only_the_last_lines_when_output_exceeds_the_limit",
        ),
        ("test_tail_bounding.py", "test_tail_is_empty_when_the_gate_prints_nothing"),
        ("test_verifier_runner.py", "test_tail_lines_reaches_gate_execution"),
    ),
    "REQ-006": (
        (
            "test_duration.py",
            "test_executed_gate_carries_duration_into_the_gate_result",
        ),
        ("test_duration.py", "test_skipped_gate_leaves_duration_unset"),
        (
            "test_duration_failing_gate.py",
            "test_failing_gate_carries_duration_into_the_gate_result",
        ),
    ),
    "REQ-007": (
        ("test_diffstats.py", "test_uncommitted_changes_report_actual_counts"),
        ("test_diffstats.py", "test_clean_tree_reports_zeroes"),
        ("test_verifier_runner.py", "test_diff_stats_reflect_working_tree"),
    ),
    "REQ-008": (
        ("test_aggregate.py", "test_any_fail_gate_makes_overall_fail"),
        ("test_aggregate.py", "test_empty_gate_list_makes_overall_pass"),
        ("test_verifier_runner.py", "test_single_failing_gate_makes_overall_fail"),
    ),
    "REQ-009": (
        (
            "test_mutation_freedom.py",
            "test_typical_gate_set_leaves_porcelain_unchanged",
        ),
        ("test_mutation_freedom.py", "test_tracked_file_mutation_is_detected"),
        ("test_mutation_freedom.py", "test_write_into_ignored_dir_is_not_a_mutation"),
    ),
    "REQ-010": (
        ("test_no_agent_cli.py", "test_actual_verifier_package_is_free_of_agent_cli"),
        (
            "test_no_agent_cli_allowlist.py",
            "test_scan_source_flags_sibling_disputatio_packages",
        ),
        ("test_capture.py", "test_shell_constructs_are_not_interpreted_by_the_runner"),
    ),
    "REQ-011": (
        ("test_robustness.py", "test_missing_binary_becomes_skip_without_raising"),
        ("test_robustness.py", "test_broken_gate_does_not_stop_the_remaining_gates"),
        ("test_verifier_port.py", "test_realistic_gate_mix_produces_a_full_report"),
    ),
}

_EXPECTED_REQUIREMENTS: Final = tuple(f"REQ-{n:03d}" for n in range(1, 12))

# Бинарь, которого заведомо нет в PATH: gate с ним обязан стать skip'ом, а
# не уронить verify() ([REQ-011]).
_MISSING_BINARY: Final = "disputatio-verifier-missing-binary"


def _runner_class() -> Any:
    """Возвращает `VerifierRunner` из пакета; отсутствие — `AssertionError`."""
    try:
        from disputatio.verifier import VerifierRunner
    except ImportError as exc:  # пакет ещё не собран — red-фаза
        raise AssertionError(
            "disputatio.verifier не экспортирует VerifierRunner"
        ) from exc
    return VerifierRunner


def _python_cmd(code: str) -> str:
    """Собирает команду `<интерпретатор> -c <code>` с корректным quoting'ом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _test_names(module: str) -> frozenset[str]:
    """Имена всех `def test_*` модуля из `tests/verifier/` по его AST.

    AST, а не импорт модуля: разбор не выполняет код тестов и не зависит от
    того, собирается ли модуль в текущем окружении.
    """
    tree = ast.parse((_TESTS_DIR / module).read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


def _evidence_json(kind: str, task_id: str) -> dict[str, Any] | None:
    """Читает claim/verdict/waiver задачи; отсутствие файла — `None`."""
    path = _REPO_ROOT / "spec" / ".tdd-evidence" / kind / _NAMESPACE / f"{task_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def test_realistic_gate_mix_produces_a_full_report(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """§6: e2e — pass + fail + два вида skip дают полный `VerificationReport`.

    Набор реалистичен по составу ролей: зелёные тесты, красный линт,
    выключенный в конфиге type-check и gate, чей бинарь не установлен.
    Проверяется вся форма отчёта, а не отдельное поле: схема, раунд,
    агрегат, порядок gates, статусы, коды возврата, хвосты, причины,
    длительности и `diff_stats` рабочего дерева ([REQ-001]…[REQ-008],
    [REQ-011]).

    Отчёт дополнительно прогоняется через сериализацию и ревалидацию:
    w-events пишет на диск именно `model_dump(by_alias=True)`, и артефакт,
    который не переживает round-trip, до `verification.json` не доедет.
    """
    runner_class = _runner_class()
    (tmp_git_repo / "tracked.txt").write_text("mutated\nsecond\n", encoding="utf-8")
    specs = [
        make_gate_spec(name="tests", cmd=_python_cmd("print('ok')")),
        make_gate_spec(
            name="lint",
            cmd=_python_cmd(
                "import sys; print('boom', file=sys.stderr); raise SystemExit(2)"
            ),
        ),
        make_gate_spec(name="types", cmd="pyrefly check", enabled=False),
        make_gate_spec(name="build", cmd=f"{_MISSING_BINARY} --version"),
    ]

    report = runner_class(specs, tmp_git_repo).verify(3)

    assert isinstance(runner_class(specs, tmp_git_repo), Verifier)
    assert report.schema_ == SCHEMA_V1
    assert report.round == 3
    assert report.overall == OverallStatus.FAIL
    assert [gate.name for gate in report.gates] == ["tests", "lint", "types", "build"]
    assert [gate.cmd for gate in report.gates] == [spec.cmd for spec in specs]
    assert [gate.status for gate in report.gates] == [
        GateStatus.PASS,
        GateStatus.FAIL,
        GateStatus.SKIP,
        GateStatus.SKIP,
    ]
    assert [gate.exit_code for gate in report.gates] == [0, 2, None, None]
    assert report.gates[0].tail == "ok"
    assert report.gates[1].tail == "boom"
    assert [gate.reason for gate in report.gates[:2]] == [None, None]
    # Оба skip'а объясняются: выключенный в конфиге и незапустившийся —
    # разные причины, и обе обязаны быть непустыми ([REQ-004]).
    assert report.gates[2].reason
    assert report.gates[3].reason
    assert report.gates[2].reason != report.gates[3].reason
    assert _MISSING_BINARY in (report.gates[3].reason or "")
    # Запущенные gates измерены, пропущенные — нет ([REQ-006]).
    for gate in report.gates[:2]:
        assert gate.duration_s is not None and gate.duration_s >= 0.0
    assert [gate.duration_s for gate in report.gates[2:]] == [None, None]
    assert (report.diff_stats.files, report.diff_stats.insertions) == (1, 2)
    assert report.diff_stats.deletions == 1

    dumped = report.model_dump(mode="json", by_alias=True)
    assert dumped["schema"] == "disputatio/v1"
    assert VerificationReport.model_validate(dumped) == report


def test_requirement_coverage_matrix_points_at_existing_tests() -> None:
    """§6.1: каждое [REQ-001]…[REQ-011] названо и покрыто существующим тестом.

    Проверяются обе стороны таблицы: ни одно требование не потеряно (ключи
    совпадают с [REQ-001]…[REQ-011]) и ни одна ссылка не висит в пустоту —
    переименованный или удалённый покрывающий тест валит приёмку, а не
    тихо превращает traceability в устаревшую прозу.
    """
    assert tuple(sorted(_REQUIREMENT_COVERAGE)) == _EXPECTED_REQUIREMENTS

    names_by_module: dict[str, frozenset[str]] = {}
    dangling: list[str] = []
    for req, pairs in sorted(_REQUIREMENT_COVERAGE.items()):
        assert pairs, f"{req}: не назван ни один покрывающий тест"
        for module, test_name in pairs:
            if module not in names_by_module:
                names_by_module[module] = _test_names(module)
            if test_name not in names_by_module[module]:
                dangling.append(f"{req} → {module}::{test_name}")

    assert not dangling, "покрывающие тесты не найдены: " + ", ".join(dangling)


def test_workstream_evidence_is_complete() -> None:
    """NFR-002: TDD-цикл каждой задачи workstream'а зафиксирован на диске.

    Закрытые задачи (TASK-001…TASK-010) обязаны иметь и evidence запуска
    (claim честного red-цикла либо одобренный человеком waiver), и
    вынесенный вердикт `PASS`/`WAIVED`. Текущая, приёмочная задача
    проверяется только по claim'у: её вердикт гейт выносит уже после
    прогона этого теста.

    Пути читаются от корня репозитория, поэтому в replay-worktree'е
    red-чекпоинта тест видит evidence того коммита, а не рабочего дерева.
    Отсутствие файла проверяется через `is_file()` — падение обязано быть
    `AssertionError`, а не `FileNotFoundError`.
    """
    assert (_REPO_ROOT / "pyproject.toml").is_file(), (
        f"корень репозитория определён неверно: {_REPO_ROOT}"
    )

    unclosed: list[str] = []
    for task_id in _CLOSED_TASKS:
        if _evidence_json("claims", task_id) is None:
            waiver = _evidence_json("waivers", task_id)
            if waiver is None or waiver.get("approved_by") != "human":
                unclosed.append(f"{task_id}: нет ни claim'а, ни waiver'а человека")
                continue
        verdict = _evidence_json("verdicts", task_id)
        if verdict is None:
            unclosed.append(f"{task_id}: нет вердикта")
        elif verdict.get("verdict") not in ("PASS", "WAIVED"):
            unclosed.append(f"{task_id}: вердикт {verdict.get('verdict')!r}")

    assert not unclosed, "незакрытые задачи workstream'а: " + "; ".join(unclosed)
    assert _evidence_json("claims", _ACCEPTANCE_TASK) is not None, (
        f"{_ACCEPTANCE_TASK}: нет claim'а приёмочной задачи — red-цикл не зафиксирован"
    )
