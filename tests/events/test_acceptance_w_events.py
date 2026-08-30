"""Приёмка workstream'а `w-events`: TASK-009, [REQ-001]…[REQ-012].

У приёмочной задачи нет продуктового кода, поэтому её единственный tracked-
артефакт — сам набор evidence. Селектор `test_workstream_evidence_is_complete`
требует claim-ы TASK-001…TASK-009 включая свой собственный: гейт пишет claim
TASK-009 уже ПОСЛЕ red-коммита, поэтому в red-чекпоинте его нет (честный red),
а в рабочем дереве есть.

Остальные тесты фиксируют, что набор не разъехался: матрица REQ→тест резолвится
через `ast`, claim-селекторы указывают на реально существующие тесты, вердикты
подтверждают red→green именно для того red-коммита, что записан в claim.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

WORKSTREAM = "ws-w-events"
TESTS_DIR_NAME = "tests/events"

COMPLETED_TASKS = tuple(f"TASK-{n:03d}" for n in range(1, 9))
ACCEPTANCE_TASK = "TASK-009"
ALL_TASKS = (*COMPLETED_TASKS, ACCEPTANCE_TASK)
ALL_REQUIREMENTS = tuple(f"REQ-{n:03d}" for n in range(1, 13))

POST_WAVE_MODULES = frozenset(
    {
        "test_artifact_root.py",
        "test_pipeline_store.py",
        "test_integrity_anchor_corruption.py",
    }
)
"""Модули `tests/events/`, пришедшие ПОСЛЕ приёмки волны 1.

`test_artifact_root.py` пинит разделение `workspace_root`/`artifact_root`
(SPEC-002 §4.1), `test_pipeline_store.py` — хранилище манифеста, пути
пайплайна, журнал событий и анкер целостности (§4.1, §4.2, P8, P9),
`test_integrity_anchor_corruption.py` — строгость читателя анкера к
повреждённой записи (§8.1 шаг 0, P9). Все трое —
требования другой спеки, у которой ни claim'ов TASK-001…009, ни REQ-001…012
нет и быть не может. Список именной, а не шаблон вроде «новее такой-то даты»:
пропуск по признаку сделал бы приёмку волны 1 необязательной для любого
нового модуля, а она обязана оставаться закрытой.
"""

# REQ → тесты, которые его пиньят. Пары `(модуль, имя теста)`, а не node-id:
# implicit-concat строк внутри коллекции валит `ruff check` (ISC004).
REQUIREMENT_COVERAGE: dict[str, tuple[tuple[str, str], ...]] = {
    "REQ-001": (
        ("test_atomic_write.py", "test_atomic_write_creates_file_with_str_content"),
        ("test_atomic_write.py", "test_temp_file_created_in_same_parent_directory"),
        (
            "test_atomic_write.py",
            "test_atomic_write_failure_leaves_previous_content_intact",
        ),
    ),
    "REQ-002": (
        ("test_bootstrap.py", "test_bootstrap_session_creates_session_directories"),
        ("test_bootstrap.py", "test_bootstrap_session_is_idempotent"),
        (
            "test_event_sink_durability.py",
            "test_emit_without_bootstrap_raises_and_creates_nothing",
        ),
    ),
    "REQ-003": (
        ("test_bootstrap.py", "test_write_config_snapshot_writes_toml_text_verbatim"),
        (
            "test_bootstrap.py",
            "test_write_config_snapshot_failure_leaves_previous_snapshot_intact",
        ),
    ),
    "REQ-004": (
        ("test_state_store.py", "test_save_then_load_roundtrip"),
        (
            "test_state_store_atomicity.py",
            "test_save_failure_at_rename_leaves_previous_state_intact",
        ),
        (
            "test_state_store_atomicity.py",
            "test_save_replaces_session_json_by_rename_without_leftovers",
        ),
    ),
    "REQ-005": (
        ("test_state_store.py", "test_load_missing_session_raises_keyerror"),
        ("test_state_store.py", "test_load_session_id_mismatch_raises_keyerror"),
        (
            "test_state_store.py",
            "test_load_schema_invalid_payload_raises_validation_error",
        ),
    ),
    "REQ-006": (
        ("test_event_sink.py", "test_emit_appends_single_line"),
        ("test_event_sink.py", "test_emit_order_matches_call_order"),
        ("test_event_sink_durability.py", "test_emit_opens_descriptor_with_o_append"),
    ),
    "REQ-007": (
        (
            "test_event_sink.py",
            "test_emit_after_partial_line_does_not_raise_and_appends",
        ),
        ("test_event_sink_durability.py", "test_emit_flushes_before_fsync"),
    ),
    "REQ-008": (
        ("test_round_artifacts.py", "test_write_creates_round_dir_and_file"),
        ("test_paths.py", "test_round_dir_zero_padded"),
    ),
    "REQ-009": (
        (
            "test_round_artifacts.py",
            "test_finalize_then_write_raises_round_immutable_error",
        ),
        (
            "test_round_artifact_names.py",
            "test_relative_path_in_name_cannot_touch_finalized_round",
        ),
    ),
    "REQ-010": (
        ("test_export.py", "test_write_result_writes_each_file_atomically"),
        ("test_export.py", "test_manifest_json_includes_sha256_per_file"),
        ("test_export.py", "test_partial_export_converged_false_with_open_issues"),
        (
            "test_export_manifest_guards.py",
            "test_manifest_name_is_reserved_case_insensitively",
        ),
        (
            "test_export_manifest_guards.py",
            "test_unserializable_manifest_rejected_before_any_file_is_written",
        ),
    ),
    "REQ-011": (
        ("test_state_store.py", "test_save_serializes_by_alias_with_schema_key"),
        ("test_init.py", "test_store_and_sink_satisfy_contracts_ports_via_package"),
    ),
    "REQ-012": (
        (
            "test_round_artifacts.py",
            "test_write_round_artifact_proposal_md_written_verbatim",
        ),
    ),
}

# NFR-001: temp-файл рядом с целевым путём, а не в другом mount point.
NFR_COVERAGE: dict[str, tuple[tuple[str, str], ...]] = {
    "NFR-001": (
        ("test_atomic_write.py", "test_temp_file_created_in_same_parent_directory"),
    ),
}

# M1–M5 из «Критериев приёмки по этапам» requirements.
MILESTONE_MODULES: dict[str, tuple[str, ...]] = {
    "M1": ("test_atomic_write.py", "test_bootstrap.py"),
    "M2": ("test_state_store.py", "test_state_store_atomicity.py"),
    "M3": ("test_event_sink.py", "test_event_sink_durability.py"),
    "M4": ("test_round_artifacts.py", "test_round_artifact_names.py"),
    "M5": ("test_export.py", "test_export_manifest_guards.py"),
}


def _repo_root() -> Path:
    """Корень репозитория ищем по `pyproject.toml`, а не по `parents[N]`."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise AssertionError("не найден корень репозитория (нет pyproject.toml)")


REPO_ROOT = _repo_root()
TESTS_DIR = REPO_ROOT / TESTS_DIR_NAME
CLAIMS_DIR = REPO_ROOT / "spec" / ".tdd-evidence" / "claims" / WORKSTREAM
VERDICTS_DIR = REPO_ROOT / "spec" / ".tdd-evidence" / "verdicts" / WORKSTREAM


def _load_json(path: Path) -> dict[str, Any]:
    """Читает evidence-файл. Отсутствие файла — AssertionError, не OSError."""
    assert path.is_file(), f"нет файла evidence: {path}"
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _test_names(module_name: str) -> frozenset[str]:
    """Имена `test_*` в модуле — статически, без импорта модуля."""
    path = TESTS_DIR / module_name
    assert path.is_file(), f"нет тест-модуля {TESTS_DIR_NAME}/{module_name}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    )


def test_workstream_evidence_is_complete() -> None:
    """Селектор TASK-009: claim есть у каждой задачи ws, включая приёмочную."""
    missing = [t for t in ALL_TASKS if not (CLAIMS_DIR / f"{t}.json").is_file()]
    assert missing == [], f"нет claim-ов {WORKSTREAM}: {missing}"


def test_every_claim_records_an_honest_red_cycle() -> None:
    """Каждый claim — отдельный red-коммит поверх своего baseline."""
    for task in COMPLETED_TASKS:
        claim = _load_json(CLAIMS_DIR / f"{task}.json")
        assert claim["schema"] == "tdd-claim/v1", task
        assert claim["task_id"] == task
        assert claim["test_path"].startswith(f"{TESTS_DIR_NAME}/"), task
        assert claim["selector"].startswith(f"{claim['test_path']}::"), task
        red, baseline = claim["red_sha"], claim["baseline_sha"]
        assert red and baseline, task
        assert red != baseline, f"{task}: red-чекпоинт совпал с baseline"


def test_verdicts_confirm_red_replay_before_green() -> None:
    """Вердикт подтверждает red→green для того самого red-коммита из claim."""
    for task in COMPLETED_TASKS:
        verdict = _load_json(VERDICTS_DIR / f"{task}.json")
        claim = _load_json(CLAIMS_DIR / f"{task}.json")
        assert verdict["verdict"] == "PASS", task
        assert verdict["red_replay"] == "EXPECTED_FAIL", task
        assert verdict["selector_at_head"] == "PASS", task
        assert verdict["red_sha"] == claim["red_sha"], f"{task}: вердикт не от claim"


def test_claim_selectors_resolve_to_existing_tests() -> None:
    """Не-вакуумность evidence: каждый селектор указывает на живой тест."""
    claims = sorted(CLAIMS_DIR.glob("TASK-*.json"))
    assert len(claims) >= len(COMPLETED_TASKS), f"claim-ов меньше ожидаемого: {claims}"
    for path in claims:
        claim = _load_json(path)
        module_part, _, test_name = str(claim["selector"]).partition("::")
        module_name = Path(module_part).name
        assert test_name in _test_names(module_name), (
            f"{path.name}: селектор не резолвится — {claim['selector']}"
        )


def test_every_requirement_is_covered_by_a_named_test() -> None:
    """Матрица REQ→тест полна и указывает на существующие тесты."""
    assert tuple(sorted(REQUIREMENT_COVERAGE)) == ALL_REQUIREMENTS
    for req, pairs in {**REQUIREMENT_COVERAGE, **NFR_COVERAGE}.items():
        assert pairs, f"{req}: пустое покрытие"
        for module_name, test_name in pairs:
            assert test_name in _test_names(module_name), (
                f"{req}: нет теста {module_name}::{test_name}"
            )


def test_every_milestone_module_is_present_and_non_empty() -> None:
    """M1–M5: каждый модуль этапа существует и содержит тесты."""
    for milestone, modules in MILESTONE_MODULES.items():
        for module_name in modules:
            assert _test_names(module_name), f"{milestone}: {module_name} без тестов"


def test_no_test_module_is_orphaned_from_evidence() -> None:
    """Каждый тест-модуль либо заклеймлен задачей, либо стоит в матрице REQ.

    Модули более поздних спек из проверки исключены поимённо
    (`POST_WAVE_MODULES`): evidence волны 1 закрыта и переписыванию не
    подлежит, а вписать SPEC-002 в матрицу REQ-001…REQ-012 значило бы выдать
    его покрытие за покрытие принятой волны.
    """
    on_disk = {path.name for path in TESTS_DIR.glob("test_*.py")} - POST_WAVE_MODULES
    claimed = {
        Path(str(_load_json(path)["test_path"])).name
        for path in CLAIMS_DIR.glob("TASK-*.json")
    }
    charted = {
        module_name
        for pairs in REQUIREMENT_COVERAGE.values()
        for module_name, _ in pairs
    }
    orphans = on_disk - claimed - charted
    assert orphans == set(), f"тест-модули вне evidence и матрицы: {sorted(orphans)}"
