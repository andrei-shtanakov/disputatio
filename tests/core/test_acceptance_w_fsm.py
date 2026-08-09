"""Приёмка workspace w-fsm: TASK-012, [NFR-001], [NFR-002], [NFR-004].

Три наблюдаемых свойства репозитория на момент закрытия волны:

1. Полнота TDD-evidence в `spec/.tdd-evidence/*/ws-w-fsm/**` — claim'ы
   TASK-001…TASK-012 и verdict'ы TASK-001…TASK-011 (TASK-008 закрыта
   оператором waiver'ом: verdict `WAIVED`, claim по ней не пишется).
   Verdict самой TASK-012 пишет `tdd_gate verify` ПОСЛЕ этого теста и
   потому здесь не проверяется — паттерн приёмки w-contracts
   (`test_acceptance_revision3.py`). Red-селектор падает assertion'ом на
   отсутствующем claim'е TASK-012: гейт создаёт его уже после прогона.
2. Матрица [REQ-001]…[REQ-015] → конкретные тесты `tests/core/**`: каждое
   требование сопоставлено существующим тест-функциям (имена собираются
   `ast.parse` по исходникам, без запуска pytest — [NFR-003]).
3. Границы workspace [NFR-004]: в `disputatio.core` нет run-loop/resume —
   ни модулей, ни функций/методов с такими именами; состав модулей ядра
   запинен.

Все проверки читают файлы репозитория (как `test_purity.py`) и ничего не
пишут: ни диска на запись, ни subprocess, ни сети.
"""

import ast
import json
from pathlib import Path
from typing import Final

_ROOT: Final[Path] = Path(__file__).parents[2]
_EVIDENCE: Final[Path] = _ROOT / "spec" / ".tdd-evidence"
_NAMESPACE: Final[str] = "ws-w-fsm"
_CORE_DIR: Final[Path] = _ROOT / "src" / "disputatio" / "core"
_TESTS_DIR: Final[Path] = Path(__file__).parent

# Задачи с честным red/green-циклом: claim + verdict PASS.
_RED_GREEN_TASKS: Final[tuple[str, ...]] = (
    "TASK-001",
    "TASK-002",
    "TASK-003",
    "TASK-004",
    "TASK-005",
    "TASK-006",
    "TASK-007",
    "TASK-009",
    "TASK-010",
    "TASK-011",
)
# TASK-008: операторский waiver вместо red-цикла (опечатка в тесте, п.4
# конституции) — verdict WAIVED, claim отсутствует по устройству гейта.
_WAIVED_TASK: Final[str] = "TASK-008"
# Приёмочная задача: claim пишет `tdd_gate red` этого же цикла.
_ACCEPTANCE_TASK: Final[str] = "TASK-012"

# Матрица трассируемости [REQ] → тесты `tests/core/**`: требование →
# файл → имена тест-функций в нём.
REQ_TO_TESTS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "REQ-001": {
        "test_transitions.py": (
            "test_full_graph_is_pinned",
            "test_allowed_edges_pass_check",
            "test_forbidden_edges_raise_invalid_transition",
            "test_terminal_phases_have_no_outgoing_edges",
            "test_transitions_total_over_session_phase",
        ),
        "test_init.py": ("test_public_names_importable_from_package",),
    },
    "REQ-002": {
        "test_writers.py": (
            "test_active_writer_map_is_pinned",
            "test_proposing_writer_is_author",
            "test_non_proposing_active_phases_writer_is_orchestrator",
            "test_idle_and_terminal_phases_have_no_writer",
            "test_at_most_one_writer_per_phase",
            "test_active_writer_total_over_session_phase",
        ),
    },
    "REQ-003": {
        "test_machine.py": (
            "test_save_precedes_emit_on_transition",
            "test_failing_store_aborts_transition",
            "test_invalid_transition_touches_nothing",
        ),
        "test_retry.py": (
            "test_third_consecutive_schema_invalid_fails_with_write_ahead",
        ),
        "test_apply_decision.py": (
            "test_converged_produces_valid_decision_and_reaches_exporting",
        ),
    },
    "REQ-004": {
        "test_machine.py": (
            "test_state_change_event_is_single_and_pinned",
            "test_save_precedes_emit_on_transition",
        ),
        "test_apply_decision.py": (
            "test_deadlock_and_budget_hit_reach_exporting_via_escalated",
        ),
    },
    "REQ-005": {
        "test_purity.py": (
            "test_actual_core_package_has_no_purity_violations",
            "test_actual_core_modules_never_call_open_or_datetime_now_today",
            "test_check_purity_dynamically_discovers_new_module_with_bad_import",
        ),
        "test_fakes.py": (
            "test_fakes_satisfy_runtime_checkable_ports",
            "test_fakes_do_not_write_to_disk",
        ),
    },
    "REQ-006": {
        "test_deciding.py": (
            "test_decide_converged_wins_over_budget_hit",
            "test_decide_budget_hit_wins_over_oscillation",
            "test_decide_oscillation_wins_over_max_rounds",
            "test_decide_continue_when_nothing_triggers",
        ),
    },
    "REQ-007": {
        "test_deciding.py": (
            "test_converged_true_for_approve_pass_no_blocker_round_two",
            "test_converged_false_when_verification_overall_fail",
            "test_converged_true_for_analyze_with_empty_gates",
            "test_converged_false_when_carried_issue_is_blocker",
        ),
    },
    "REQ-008": {
        "test_deciding.py": (
            "test_anti_sycophancy_round_one_develop_approve_does_not_trigger",
            "test_anti_sycophancy_round_one_analyze_empty_patch_converges",
            "test_anti_sycophancy_round_one_analyze_nonempty_patch_does_not_converge",
            "test_anti_sycophancy_round_two_approve_converges",
            "test_decide_anti_sycophancy_round_one_forces_continue",
        ),
    },
    "REQ-009": {
        "test_deciding.py": (
            "test_decide_continue_when_verification_fails_and_review_requests_changes",
            "test_converged_false_when_verification_overall_fail",
        ),
    },
    "REQ-010": {
        "test_deciding.py": (
            "test_decide_budget_boundary_tokens_equal_limit_does_not_trigger",
            "test_decide_budget_hit_tokens_over_limit",
            "test_decide_budget_hit_wall_seconds_over_limit",
        ),
    },
    "REQ-011": {
        "test_deciding.py": (
            "test_decide_oscillation_diff_similarity_triggers_deadlock",
            # Имя длиннее 88 - конкатенация литералов держит строку в лимите.
            (
                "test_decide_oscillation_diff_similarity_exactly_threshold"
                "_does_not_trigger"
            ),
            "test_decide_oscillation_diff_skipped_when_patch_two_back_is_none",
            "test_decide_oscillation_repeated_issue_third_time_triggers_deadlock",
            "test_decide_oscillation_repeated_issue_second_time_does_not_trigger",
        ),
        "test_oscillation.py": (
            "test_patch_similarity_identical_patches_is_one",
            "test_patch_similarity_exactly_at_threshold_does_not_trigger",
            "test_find_repeated_issue_third_occurrence_returns_issue",
            "test_find_repeated_issue_second_occurrence_returns_none",
        ),
    },
    "REQ-012": {
        "test_deciding.py": (
            "test_decide_max_rounds_deadlock_when_round_equals_max_rounds",
            "test_decide_continue_when_round_below_max_rounds",
        ),
    },
    "REQ-013": {
        "test_apply_decision.py": (
            "test_converged_produces_valid_decision_and_reaches_exporting",
            "test_deadlock_and_budget_hit_reach_exporting_via_escalated",
            "test_is_partial_pinned_for_each_outcome",
            "test_continue_advances_round_and_carries_directive",
        ),
    },
    "REQ-014": {
        "test_retry.py": (
            "test_two_schema_invalid_calls_stay_below_limit_and_touch_nothing",
            "test_third_consecutive_schema_invalid_fails_with_write_ahead",
            "test_handle_step_success_resets_retry_counter",
            "test_successful_transition_resets_retry_counter",
        ),
    },
    "REQ-015": {
        "test_transitions.py": (
            "test_forbidden_edges_raise_invalid_transition",
            "test_check_transition_does_not_mutate_graph",
        ),
        "test_machine.py": ("test_revise_loop_increments_round",),
        "test_apply_decision.py": (
            "test_continue_advances_round_and_carries_directive",
            "test_continue_without_directive_raises_and_touches_nothing",
        ),
        "test_oscillation.py": ("test_find_repeated_issue_does_not_mutate_inputs",),
    },
}

# Состав модулей ядра волны 1: run-loop/resume — волна 2 (w-runtime).
EXPECTED_CORE_MODULES: Final[frozenset[str]] = frozenset(
    {"__init__", "deciding", "machine", "oscillation", "transitions", "writers"}
)
FORBIDDEN_NAME_PREFIXES: Final[tuple[str, ...]] = ("run", "resume")


def _verdict(task_id: str) -> str:
    """Читает поле `verdict` из verdict-файла задачи `task_id`."""
    path = _EVIDENCE / "verdicts" / _NAMESPACE / f"{task_id}.json"
    return str(json.loads(path.read_text(encoding="utf-8"))["verdict"])


def _defined_names(source: Path) -> set[str]:
    """Все имена функций/классов (включая методы) в модуле `source`."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def test_w_fsm_evidence_complete() -> None:
    """Claim'ы TASK-001…012 на месте; verdict'ы — PASS, TASK-008 — WAIVED."""
    claims_dir = _EVIDENCE / "claims" / _NAMESPACE
    missing_claims = [
        task_id
        for task_id in (*_RED_GREEN_TASKS, _ACCEPTANCE_TASK)
        if not (claims_dir / f"{task_id}.json").is_file()
    ]
    assert missing_claims == []
    verdicts = {task_id: _verdict(task_id) for task_id in _RED_GREEN_TASKS}
    assert verdicts == dict.fromkeys(_RED_GREEN_TASKS, "PASS")
    waiver = _EVIDENCE / "waivers" / _NAMESPACE / f"{_WAIVED_TASK}.json"
    assert waiver.is_file()
    assert _verdict(_WAIVED_TASK) == "WAIVED"


def test_req_test_matrix_covers_every_requirement() -> None:
    """Каждое [REQ-001]…[REQ-015] сопоставлено существующим тестам ядра."""
    expected_reqs = [f"REQ-{number:03d}" for number in range(1, 16)]
    assert sorted(REQ_TO_TESTS) == expected_reqs
    assert [req for req, files in REQ_TO_TESTS.items() if not files] == []
    names_by_file = {
        path.name: _defined_names(path) for path in sorted(_TESTS_DIR.glob("test_*.py"))
    }
    unknown = [
        f"{req}: {file_name}::{test_name}"
        for req, files in REQ_TO_TESTS.items()
        for file_name, test_names in files.items()
        for test_name in test_names
        if test_name not in names_by_file.get(file_name, set())
    ]
    assert unknown == []


def test_core_has_no_run_loop_or_resume() -> None:
    """Границы [NFR-004]: состав модулей ядра запинен, run-loop/resume нет."""
    modules = {path.stem for path in _CORE_DIR.glob("*.py")}
    assert modules == set(EXPECTED_CORE_MODULES)
    offenders = [
        f"{path.name}::{name}"
        for path in sorted(_CORE_DIR.glob("*.py"))
        for name in sorted(_defined_names(path))
        if name.lstrip("_").lower().startswith(FORBIDDEN_NAME_PREFIXES)
    ]
    assert offenders == []
