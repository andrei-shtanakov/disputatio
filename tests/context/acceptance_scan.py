"""Сканеры итоговой приёмки воркстрима `w-context` (TASK-008).

Не тестовый модуль: префикса `test_` нет, pytest его не собирает. Здесь
живут чистые функции и таблицы, которыми `test_acceptance_w_context.py`
проверяет три независимые вещи:

* **чистота пакета** — `src/disputatio/context/**` не импортирует ничего,
  что даёт I/O, время или случайность, и не прячет такой импорт за
  `__import__`, относительным импортом или `from x import *` (NFR-001);
* **границы зависимостей** — пакет опирается только на stdlib и
  `disputatio.contracts.*`; соседние пакеты оркестратора ему недоступны;
* **трассируемость** — таблица [REQ-001]…[REQ-011] → закрывающие тест-кейсы
  и её обратная сторона: ни один тест-модуль воркстрима не остался вне
  таблицы и вне claim-ов (orphan-детектор).

Разбор синтаксический (`ast.parse`) — ничего не исполняется и не
импортируется. Отсюда известное ограничение: динамический `getattr` по
строковому имени модуля сканер не увидит. Это принято сознательно —
критерий приёмки сформулирован про явные импорты и явные вызовы, а
исполнять код пакета ради более сильной проверки значит проверять другую
вещь.

Матрица покрытия хранит пары `(модуль, тест)`, а не готовые node-id:
implicit-concat длинных строк внутри коллекции валит `ruff` (ISC004), а
пары ещё и переживают перенос каталога тестов.
"""

import ast
import fnmatch
import json
import sys
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path

from disputatio import context

NAMESPACE = "ws-w-context"
"""Каталог evidence этого воркстрима внутри `spec/.tdd-evidence/`."""

CONTEXT_TASKS: tuple[str, ...] = tuple(f"TASK-{number:03d}" for number in range(1, 9))
"""TASK-001…008 — все leaf-задачи воркстрима, включая саму приёмку."""

VERIFIED_TASKS: tuple[str, ...] = CONTEXT_TASKS[:-1]
"""Задачи с уже записанным вердиктом.

TASK-008 сюда не входит: её вердикт harness пишет ПОСЛЕ этого прогона, и
требовать его — значит требовать невозможного. Именно поэтому приёмка
честно краснеет: на момент `tdd_gate red` нет ещё и собственного claim-а.
"""

PASS_VERDICT = "PASS"
WAIVED_VERDICT = "WAIVED"
MISSING_VERDICT = "MISSING"
"""Подставляется вместо статуса, когда файла вердикта нет вовсе."""

HONEST_RED_REPLAY = "EXPECTED_FAIL"
HONEST_SELECTOR_AT_HEAD = "PASS"
"""Как выглядит подтверждённый red → green цикл в вердикте."""

PACKAGE_ROOT = "disputatio"

ALLOWED_INTERNAL_PREFIXES: tuple[str, ...] = (
    "disputatio.context",
    "disputatio.contracts",
)
"""Единственные внутренние зависимости пакета: он сам и контракты.

`core`, `adapters`, `verifier` сюда не входят намеренно: сборка промпта —
чистая функция от артефактов, и любая ссылка на оркестратор развернула бы
зависимость в обратную сторону.
"""

FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "datetime",
        "httpx",
        "importlib",
        "io",
        "os",
        "pathlib",
        "random",
        "requests",
        "secrets",
        "socket",
        "subprocess",
        "time",
    }
)
"""Импорты, любой из которых ломает NFR-001/NFR-002.

`importlib` в списке по той же причине, что и `__import__`: это второй
способ втянуть запрещённый модуль так, чтобы проверка по именам импортов
его не увидела.
"""

FORBIDDEN_CALLS: frozenset[str] = frozenset({"open", "__import__"})
"""Вызовы-обходы: файл можно открыть, ничего не импортировав."""

EXEMPT_TEST_MODULES: frozenset[str] = frozenset()
"""Тест-модули, сознательно оставленные вне матрицы покрытия.

Пусто и должно оставаться пустым: исключение — это признание, что тест
ничего не закрывает.
"""

TESTS_DIR_NAME = "tests/context"
"""Префикс node-id; сама матрица хранит пары `(модуль, тест)`."""

REQUIREMENT_IDS: tuple[str, ...] = tuple(f"REQ-{n:03d}" for n in range(1, 12))
NFR_IDS: tuple[str, ...] = tuple(f"NFR-{n:03d}" for n in range(1, 5))

UNSPECIFIED_NFRS: tuple[str, ...] = ("NFR-004",)
"""NFR, не определённые в spec воркстрима, — закрыть их нечем.

`spec/maestro-requirements.md` и `spec/maestro-design.md` этого воркстрима
— побайтовые копии `maestro-tasks.md`, поэтому прозы требований в нём нет
вовсе: NFR-001…003 восстанавливаются по трассировкам задач (чистота
пакета, детерминизм, минимальный публичный API), а NFR-004 не упомянут
нигде, кроме списка трассировок TASK-008. Придумать ему тест — значит
выдать домысел за покрытие; вместо этого он назван явно, и тест полноты
следит, чтобы список не разросся молча.
"""

_REQUIREMENT_COVERAGE: dict[str, tuple[tuple[str, str], ...]] = {
    "REQ-001": (
        ("test_author.py", "test_cold_start_round_one_renders_task_only"),
        ("test_author_assembly.py", "test_prompt_names_the_current_round"),
        ("test_author_assembly.py", "test_prompt_carries_task_mode"),
        ("test_author_assembly.py", "test_sections_follow_the_order_of_spec_6_1"),
        ("test_init.py", "test_public_functions_are_submodule_objects"),
    ),
    "REQ-002": (
        ("test_author.py", "test_only_carried_issues_reach_prompt_sorted_by_severity"),
        ("test_author.py", "test_carried_issue_fields_are_rendered"),
        (
            "test_author_assembly.py",
            "test_review_without_decision_renders_no_issues_section",
        ),
        ("test_sections.py", "test_select_open_issues_filters_and_sorts_by_severity"),
        ("test_sections.py", "test_select_open_issues_sort_is_stable_within_severity"),
        (
            "test_sections_rendering.py",
            "test_render_issues_section_shows_every_filled_field",
        ),
        (
            "test_sections_rendering.py",
            "test_render_issues_section_omits_unset_optional_fields",
        ),
        (
            "test_sections_rendering.py",
            "test_unknown_severity_sorts_last_without_raising",
        ),
    ),
    "REQ-003": (
        ("test_author.py", "test_only_failed_gates_reach_prompt"),
        ("test_author.py", "test_directive_is_rendered_verbatim_in_its_own_section"),
        ("test_author.py", "test_absent_directive_removes_the_whole_section"),
        ("test_sections.py", "test_select_failed_gates_keeps_only_failures"),
        ("test_sections.py", "test_render_failed_gates_section_shows_name_and_tail"),
        (
            "test_sections_blocks.py",
            "test_failed_gates_section_states_status_and_command",
        ),
    ),
    "REQ-004": (
        ("test_author.py", "test_prior_artifact_from_wrong_round_is_rejected"),
        ("test_author.py", "test_round_one_rejects_any_prior_artifact"),
        ("test_author.py", "test_signature_forbids_history_and_own_proposals"),
    ),
    "REQ-005": (
        ("test_reviewer.py", "test_artifact_paths_are_rendered_verbatim"),
        ("test_reviewer.py", "test_artifact_paths_stay_outside_artifact_tags"),
        ("test_reviewer.py", "test_signature_carries_paths_not_artifact_content"),
        ("test_reviewer.py", "test_sections_follow_the_order_of_spec_6_2"),
        ("test_reviewer.py", "test_prompt_names_the_current_round_and_task_mode"),
        (
            "test_reviewer_assembly.py",
            "test_each_material_path_is_labelled_with_its_own_kind",
        ),
        ("test_reviewer_assembly.py", "test_materials_title_opens_its_own_section"),
        ("test_reviewer_assembly.py", "test_intro_names_the_current_round"),
        ("test_reviewer_assembly.py", "test_artifact_from_a_later_round_is_rejected"),
    ),
    "REQ-006": (
        ("test_reviewer.py", "test_full_verification_report_reaches_the_reviewer"),
        ("test_reviewer.py", "test_verification_from_another_round_is_rejected"),
        (
            "test_sections.py",
            "test_render_verification_section_includes_every_gate_and_summary",
        ),
        (
            "test_sections_blocks.py",
            "test_verification_section_states_status_of_every_gate",
        ),
        (
            "test_sections_blocks.py",
            "test_verification_section_states_command_of_every_gate",
        ),
        (
            "test_sections_rendering.py",
            "test_render_gate_blocks_show_exit_code_and_reason",
        ),
    ),
    "REQ-007": (
        ("test_reviewer.py", "test_only_issues_claimed_resolved_reach_the_reviewer"),
        ("test_reviewer.py", "test_resolved_issue_fields_are_rendered_unchanged"),
        (
            "test_reviewer.py",
            "test_missing_prior_artifacts_remove_the_resolved_section",
        ),
        ("test_reviewer.py", "test_prior_artifact_from_wrong_round_is_rejected"),
        ("test_sections.py", "test_select_resolved_issues_is_exact_complement"),
    ),
    "REQ-008": (
        ("test_reviewer.py", "test_schema_requirements_present_for_any_valid_input"),
        ("test_schema_rules.py", "test_constant_exists_and_is_non_empty_text"),
        ("test_schema_rules.py", "test_rule_request_changes_requires_blocking_issue"),
        ("test_schema_rules.py", "test_rule_blocking_issue_requires_evidence"),
        (
            "test_schema_rules.py",
            "test_rule_approve_forbidden_when_verification_failed",
        ),
        ("test_schema_rules.py", "test_rule_checked_is_mandatory"),
        ("test_schema_rules.py", "test_checked_requirement_demands_non_empty_list"),
        (
            "test_schema_rules_fidelity.py",
            "test_missing_evidence_consequence_is_degradation_not_rejection",
        ),
        (
            "test_schema_rules_fidelity.py",
            "test_degraded_issue_does_not_hold_a_negative_verdict",
        ),
    ),
    "REQ-009": (
        ("test_reviewer.py", "test_signature_admits_no_free_text_of_author_answers"),
    ),
    "REQ-010": (
        ("test_tags.py", "test_wrap_puts_content_verbatim_between_tags"),
        ("test_tags.py", "test_plain_content_passes_through_unchanged"),
        ("test_tags.py", "test_literal_close_tag_in_content_does_not_close_the_block"),
        (
            "test_tags.py",
            "test_literal_open_tag_in_content_does_not_open_a_second_block",
        ),
        ("test_tags_fidelity.py", "test_tags_are_not_substrings_of_each_other"),
        ("test_tags_fidelity.py", "test_neutralisation_inserts_and_never_deletes"),
        (
            "test_tags_fidelity.py",
            "test_surrounding_whitespace_of_content_is_preserved",
        ),
        (
            "test_tags_fidelity.py",
            "test_empty_content_still_produces_a_well_formed_block",
        ),
        ("test_tags_literals.py", "test_emitted_constant_is_a_plain_string_literal"),
        (
            "test_sections.py",
            "test_non_empty_sections_wrap_artifact_text_with_title_outside",
        ),
        ("test_sections_blocks.py", "test_each_issue_gets_its_own_artifact_block"),
        ("test_sections_blocks.py", "test_each_gate_gets_its_own_artifact_block"),
        ("test_author_assembly.py", "test_user_prompt_sits_inside_artifact_tags"),
        (
            "test_hygiene.py",
            "test_close_tag_in_task_prompt_keeps_author_boundary_intact",
        ),
        (
            "test_hygiene.py",
            "test_author_boundary_survives_a_literal_tag_in_each_source",
        ),
        (
            "test_hygiene.py",
            "test_reviewer_boundary_survives_literal_tags_from_every_source",
        ),
        (
            "test_hygiene.py",
            "test_orchestrator_static_text_never_lands_inside_artifact_tags",
        ),
        ("test_boundary.py", "test_scan_accepts_two_well_formed_blocks"),
        ("test_boundary.py", "test_scan_reports_close_without_open"),
        ("test_boundary.py", "test_scan_reports_open_nested_inside_a_block"),
        ("test_boundary.py", "test_scan_reports_a_block_that_is_never_closed"),
        ("test_boundary.py", "test_contains_covers_the_block_including_its_own_tags"),
        (
            "test_boundary.py",
            "test_overlaps_catches_a_range_that_only_straddles_the_opening_tag",
        ),
        (
            "test_boundary.py",
            "test_overlaps_catches_a_range_that_only_straddles_the_closing_tag",
        ),
        ("test_boundary.py", "test_find_all_returns_overlapping_occurrences"),
    ),
    "REQ-011": (
        ("test_author.py", "test_all_optional_inputs_none_on_later_round"),
        ("test_author_assembly.py", "test_absent_sections_leave_no_blank_gap"),
        (
            "test_author_assembly.py",
            "test_blank_line_separates_sections_and_never_splits_one",
        ),
        ("test_reviewer.py", "test_absent_sections_leave_no_blank_gap"),
        ("test_sections.py", "test_render_sections_are_empty_on_empty_input"),
    ),
}
"""Traceability [REQ-001]…[REQ-011] → закрывающие тест-кейсы (≥1 на требование)."""

_NFR_COVERAGE: dict[str, tuple[tuple[str, str], ...]] = {
    "NFR-001": (
        (
            "test_acceptance_w_context.py",
            "test_context_package_stays_free_of_io_and_nondeterminism",
        ),
        (
            "test_acceptance_w_context.py",
            "test_context_package_depends_only_on_stdlib_and_contracts",
        ),
    ),
    "NFR-002": (
        ("test_tags.py", "test_wrap_is_deterministic"),
        ("test_tags_literals.py", "test_wrap_is_byte_identical_across_processes"),
        ("test_schema_rules.py", "test_constant_is_a_plain_string_literal"),
        ("test_schema_rules.py", "test_constant_is_byte_identical_across_processes"),
        (
            "test_hygiene.py",
            "test_author_prompt_is_byte_identical_for_identical_arguments",
        ),
        (
            "test_hygiene.py",
            "test_reviewer_prompt_is_byte_identical_for_identical_arguments",
        ),
        (
            "test_hygiene.py",
            "test_prompt_is_byte_identical_for_freshly_built_equal_inputs",
        ),
        ("test_hygiene.py", "test_prompt_ignores_construction_order_of_inputs"),
        ("test_hygiene.py", "test_prompt_carries_no_object_identity_or_wall_clock"),
        (
            "test_prompt_determinism.py",
            "test_prompts_are_byte_identical_across_processes",
        ),
        ("test_prompt_determinism.py", "test_probe_carries_both_issue_sections"),
    ),
    "NFR-003": (
        ("test_init.py", "test_all_lists_exactly_the_two_public_names"),
        ("test_init.py", "test_star_import_exposes_exactly_the_public_api"),
        ("test_init.py", "test_star_import_hides_internal_helpers"),
    ),
}
"""Traceability [NFR-001]…[NFR-003]; [NFR-004] — в `UNSPECIFIED_NFRS`."""


def requirement_coverage() -> dict[str, tuple[tuple[str, str], ...]]:
    """Матрица REQ → пары `(модуль, тест)`."""
    return _REQUIREMENT_COVERAGE


def nfr_coverage() -> dict[str, tuple[tuple[str, str], ...]]:
    """Матрица NFR → пары `(модуль, тест)`."""
    return _NFR_COVERAGE


def charted_modules() -> set[str]:
    """Тест-модули, названные хотя бы одной строкой матрицы покрытия."""
    return {
        module
        for coverage in (_REQUIREMENT_COVERAGE, _NFR_COVERAGE)
        for pairs in coverage.values()
        for module, _test in pairs
    }


def node_ids_of(
    coverage: dict[str, tuple[tuple[str, str], ...]],
) -> dict[str, set[str]]:
    """Матрица в виде готовых node-id pytest."""
    return {
        key: {f"{TESTS_DIR_NAME}/{module}::{name}" for module, name in pairs}
        for key, pairs in coverage.items()
    }


def outside_tests_dir(coverage: dict[str, tuple[tuple[str, str], ...]]) -> list[str]:
    """Строки матрицы, чей модуль лежит НЕ прямо в `tests/context/`.

    Проверяется вложенность после `resolve()`, а не суффикс имени: ссылка
    `../contracts/test_base.py` разобралась бы без ошибки и зачла бы
    требование чужим покрытием.
    """
    tests_dir = context_tests_dir()
    outside: list[str] = []
    for key, pairs in sorted(coverage.items()):
        for module, _name in pairs:
            path = (tests_dir / module).resolve()
            if path.parent != tests_dir or not path.name.startswith("test_"):
                outside.append(f"{key} → {module}")
    return outside


def repo_root() -> Path:
    """Корень репозитория, выведенный из расположения файла, а не из CWD."""
    return Path(__file__).resolve().parents[2]


def context_tests_dir() -> Path:
    """Каталог `tests/context/`."""
    return Path(__file__).resolve().parent


def context_package_dir() -> Path:
    """Каталог пакета `disputatio.context` (без зависимости от CWD)."""
    package_file = context.__file__
    assert package_file is not None, "у disputatio.context нет исходника на диске"
    return Path(package_file).parent


def claims_dir() -> Path:
    """Каталог claim-ов этого воркстрима."""
    return repo_root() / "spec" / ".tdd-evidence" / "claims" / NAMESPACE


def verdicts_dir() -> Path:
    """Каталог вердиктов этого воркстрима."""
    return repo_root() / "spec" / ".tdd-evidence" / "verdicts" / NAMESPACE


def evidence_json(kind: str, task_id: str) -> dict[str, object] | None:
    """Содержимое `spec/.tdd-evidence/<kind>/ws-w-context/<task>.json`."""
    path = repo_root() / "spec" / ".tdd-evidence" / kind / NAMESPACE / f"{task_id}.json"
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def missing_claims(task_ids: Iterable[str]) -> list[str]:
    """Задачи без файла claim — то есть без зафиксированного red-цикла."""
    return [task_id for task_id in task_ids if evidence_json("claims", task_id) is None]


def verdict_statuses(task_ids: Iterable[str]) -> dict[str, str]:
    """`{task_id: verdict}`; `MISSING`, если файла вердикта нет."""
    statuses: dict[str, str] = {}
    for task_id in task_ids:
        verdict = evidence_json("verdicts", task_id)
        outcome = None if verdict is None else verdict.get("verdict")
        statuses[task_id] = outcome if isinstance(outcome, str) else MISSING_VERDICT
    return statuses


def claim_selectors(task_ids: Iterable[str]) -> dict[str, str]:
    """`{task_id: selector}` — какой тест доказывал red каждой задачи."""
    selectors: dict[str, str] = {}
    for task_id in task_ids:
        claim = evidence_json("claims", task_id)
        if claim is None:
            continue
        selector = claim.get("selector")
        if isinstance(selector, str):
            selectors[task_id] = selector
    return selectors


def claimed_modules(task_ids: Iterable[str]) -> set[str]:
    """Имена тест-модулей, на которые ссылаются claim-ы воркстрима."""
    return {
        selector.rpartition("::")[0].rpartition("/")[2]
        for selector in claim_selectors(task_ids).values()
    }


def pass_verdict_defects(
    task_id: str, verdict: dict[str, object], claim: dict[str, object] | None
) -> list[str]:
    """Расхождения `PASS`-вердикта со своим claim-ом; пустой список — чисто."""
    if claim is None:
        return [f"{task_id}: вердикт PASS без claim-а"]

    defects: list[str] = []
    if verdict.get("claim_revision") != claim.get("revision"):
        defects.append(
            f"{task_id}: вердикт по ревизии {verdict.get('claim_revision')!r}, "
            f"claim — {claim.get('revision')!r}"
        )
    red_sha = verdict.get("red_sha")
    if not red_sha:
        defects.append(f"{task_id}: вердикт PASS без red_sha")
    elif red_sha != claim.get("red_sha"):
        defects.append(f"{task_id}: red_sha вердикта не совпадает с claim-ом")
    if verdict.get("red_replay") != HONEST_RED_REPLAY:
        defects.append(
            f"{task_id}: red_replay {verdict.get('red_replay')!r}, "
            f"ожидался {HONEST_RED_REPLAY!r}"
        )
    if verdict.get("selector_at_head") != HONEST_SELECTOR_AT_HEAD:
        defects.append(
            f"{task_id}: selector_at_head {verdict.get('selector_at_head')!r}, "
            f"ожидался {HONEST_SELECTOR_AT_HEAD!r}"
        )
    return defects


def collect_test_node_ids(directory: Path) -> set[str]:
    """Все node-id вида `tests/context/test_x.py::test_y` в каталоге.

    Параметризация игнорируется: pytest дописывает `[...]` к имени функции,
    а матрица traceability оперирует именем тест-кейса.
    """
    node_ids: set[str] = set()
    for path in sorted(directory.glob("test_*.py")):
        relative = path.relative_to(repo_root()).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef
            ) and node.name.startswith("test_"):
                node_ids.add(f"{relative}::{node.name}")
    return node_ids


def test_modules_on_disk(directory: Path) -> set[str]:
    """Имена файлов `test_*.py` в каталоге."""
    return {path.name for path in directory.glob("test_*.py")}


def imported_modules(source: str) -> set[str]:
    """Абсолютные dotted-имена модулей, которые втягивает исходник.

    Относительные импорты сюда не попадают: их корень по тексту неизвестен,
    поэтому они запрещены отдельно (`find_relative_imports`).
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def find_forbidden_imports(source: str) -> list[str]:
    """Импорты модулей из `FORBIDDEN_MODULES` — точечная и `from`-форма.

    Сравнивается корень dotted-имени, поэтому `import os.path` и
    `import os.path as p` ловятся наравне с `import os`.
    """
    return sorted(
        module
        for module in imported_modules(source)
        if module.partition(".")[0] in FORBIDDEN_MODULES
    )


def find_relative_imports(source: str) -> list[str]:
    """Относительные импорты: они прячут корень модуля от проверки выше."""
    return sorted(
        "." * node.level + (node.module or "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.level > 0
    )


def find_star_imports(source: str) -> list[str]:
    """`from x import *`: имена приходят в модуль, не называя себя."""
    return sorted(
        node.module or "."
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
    )


def find_forbidden_calls(source: str) -> list[str]:
    """Вызовы `open`/`__import__` — обходы, не требующие импорта вовсе."""
    return sorted(
        {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).rpartition(".")[2] in FORBIDDEN_CALLS
        }
    )


def find_dependency_violations(source: str) -> list[str]:
    """Импорты вне «stdlib + `disputatio.contracts.*`».

    Принадлежность к stdlib берётся у самого интерпретатора
    (`sys.stdlib_module_names`), а не из списка в этом файле: список
    устарел бы молча, а на пакет, зависящий от `pydantic`, приёмка обязана
    среагировать.
    """
    violations: list[str] = []
    for module in sorted(imported_modules(source)):
        root = module.partition(".")[0]
        if root == PACKAGE_ROOT:
            if not module.startswith(ALLOWED_INTERNAL_PREFIXES):
                violations.append(module)
        elif root not in sys.stdlib_module_names:
            violations.append(module)
    return violations


def find_purity_offences(source: str) -> list[str]:
    """Все нарушения чистоты модуля разом, в стабильном порядке."""
    return [
        *(f"import {name}" for name in find_forbidden_imports(source)),
        *(f"relative import {name}" for name in find_relative_imports(source)),
        *(f"star import {name}" for name in find_star_imports(source)),
        *(f"call {name}" for name in find_forbidden_calls(source)),
    ]


def scan_purity(directory: Path) -> dict[str, list[str]]:
    """`{модуль пакета: нарушения чистоты}` — пусто, если пакет чист."""
    return _scan(directory, find_purity_offences)


def scan_dependencies(directory: Path) -> dict[str, list[str]]:
    """`{модуль пакета: запрещённые зависимости}` — пусто, если чисто."""
    return _scan(directory, find_dependency_violations)


def pyrefly_config_path() -> Path:
    """Конфиг статического типизатора, действующий на весь репозиторий."""
    return repo_root() / "pyrefly.toml"


def pyrefly_relaxed_globs() -> dict[str, list[str]]:
    """`{glob: отключённые классы ошибок}` из `[[sub-config]]` pyrefly.

    Учитываются только отключения (`false`): включение проверки обратно —
    не послабление.
    """
    config = tomllib.loads(pyrefly_config_path().read_text(encoding="utf-8"))
    sub_configs = config.get("sub-config", [])
    relaxed: dict[str, list[str]] = {}
    for sub_config in sub_configs:
        matches = sub_config.get("matches")
        errors = sub_config.get("errors", {})
        disabled = sorted(name for name, value in errors.items() if value is False)
        if isinstance(matches, str) and disabled:
            relaxed[matches] = disabled
    return relaxed


def relaxed_package_modules() -> dict[str, list[str]]:
    """Модули пакета, накрытые послаблением pyrefly; пусто — значит чисто.

    «`pyrefly check` без ошибок» — критерий приёмки только пока проверка
    действительно применяется к коду пакета. Точечные послабления в
    репозитории есть (byte-locked тесты соседних воркстримов), и этот
    сканер следит, чтобы ни одно из них не расползлось на
    `src/disputatio/context/**`.
    """
    package_dir = context_package_dir()
    root = repo_root()
    covered: dict[str, list[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        globs = sorted(
            glob for glob in pyrefly_relaxed_globs() if fnmatch.fnmatch(relative, glob)
        )
        if globs:
            covered[relative] = globs
    return covered


def _scan(directory: Path, finder: Callable[[str], list[str]]) -> dict[str, list[str]]:
    """Обходит `*.py` пакета рекурсивно; ключ — путь относительно пакета."""
    found: dict[str, list[str]] = {}
    for path in sorted(directory.rglob("*.py")):
        hits = finder(path.read_text(encoding="utf-8"))
        if hits:
            found[path.relative_to(directory).as_posix()] = hits
    return found
