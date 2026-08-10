"""Сканеры итоговой приёмки воркспейса `w-adapters` (TASK-010, [REQ-013]/[REQ-014]).

Не тестовый модуль (pytest его не собирает) — набор чистых функций, которыми
`test_acceptance_w_adapters.py` проверяет три вещи разом:

* TDD-evidence: у каждой leaf-задачи есть claim (red-цикл), у закрытых — вердикт
  `PASS` ([REQ-013], INV-17);
* границы тестов: в `tests/adapters/` нет `skip`/`xfail` и нет обращений к
  настоящим бинарям `claude`/`codex` ([REQ-012], [REQ-014]);
* границы кода: `asyncio.create_subprocess_exec` живёт только в `_process.py`
  ([REQ-012], [DESIGN-002]).

Как и `gate_scan.py`, читает исходники через `ast.parse` — ничего не исполняет.
Имена вызовов сначала раскрываются по импортам модуля (`_import_bindings`),
поэтому `from subprocess import run`, `import subprocess as sp` и
`from pytest import skip` ловятся наравне с точечной записью.

Известное ограничение то же самое, что у `gate_scan.py`: проверка синтаксическая
и обходится динамическим `getattr`/`__import__`. Здесь это принимается:
acceptance-критерии [REQ-012]/[REQ-014] сформулированы про явные вызовы и явные
маркеры.
"""

import ast
import json
from collections.abc import Callable, Iterable
from pathlib import Path

from disputatio import adapters

NAMESPACE = "ws-w-adapters"
"""Каталог evidence этого воркспейса внутри `spec/.tdd-evidence/`."""

ADAPTER_TASKS: tuple[str, ...] = tuple(f"TASK-{number:03d}" for number in range(1, 11))
"""TASK-001…010 — все leaf-задачи воркспейса, включая саму приёмку."""

VERIFIED_TASKS: tuple[str, ...] = ADAPTER_TASKS[:-1]
"""Задачи с уже записанным вердиктом.

TASK-010 сюда не входит: её вердикт harness пишет ПОСЛЕ этого прогона, поэтому
требовать его — значит требовать невозможного.
"""

PASS_VERDICT = "PASS"
MISSING_VERDICT = "MISSING"
"""Подставляется вместо статуса, когда файла вердикта нет вовсе."""

SKIP_MARK_NAMES: frozenset[str] = frozenset({"skip", "skipif", "xfail", "importorskip"})
"""Атрибуты pytest, любое появление которых нарушает [REQ-014]."""

PROBE_CALLS: frozenset[str] = frozenset(
    {
        "shutil.which",
        "which",
        "os.system",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "create_subprocess_exec",
        "create_subprocess_shell",
    }
)
"""Вызовы, которыми тест мог бы дотянуться до настоящего бинаря ([REQ-012])."""

SUBPROCESS_EXEC_NAMES: frozenset[str] = frozenset(
    {"create_subprocess_exec", "create_subprocess_shell"}
)
"""Хвосты имён, разрешённые только в `_process.py` ([DESIGN-002])."""

PROCESS_SEAM_MODULE = "_process.py"
"""Единственный модуль пакета, которому можно порождать процессы."""

TESTS_DIR = "tests/adapters"
"""Префикс node-id: матрица хранит пары `(модуль, тест)`, а не готовые строки."""

REQUIREMENT_TESTS: dict[str, tuple[tuple[str, str], ...]] = {
    "REQ-002": (
        ("test_package_init.py", "test_import_disputatio_adapters_has_no_side_effects"),
        ("test_package_init.py", "test_public_names_reexported_via_all"),
    ),
    "REQ-003": (
        ("test_process_seam.py", "test_run_returns_agent_turn_from_fake_stdout"),
        (
            "test_process_seam.py",
            "test_claude_code_adapter_satisfies_agent_adapter_protocol",
        ),
    ),
    "REQ-004": (
        ("test_codex_adapter.py", "test_run_returns_agent_turn_from_fake_stdout"),
        ("test_package_init.py", "test_adapter_stubs_satisfy_agent_adapter_protocol"),
    ),
    "REQ-005": (
        ("test_permissions.py", "test_author_argv_unrestricted"),
        (
            "test_claude_code_adapter.py",
            "test_author_role_argv_has_no_allowed_tools_restriction",
        ),
        (
            "test_codex_adapter.py",
            "test_author_role_argv_has_no_allowed_tools_restriction",
        ),
    ),
    "REQ-006": (
        ("test_permissions.py", "test_reviewer_argv_granular"),
        (
            "test_claude_code_adapter.py",
            "test_reviewer_role_argv_is_granular_and_excludes_write_tools",
        ),
        (
            "test_codex_adapter.py",
            "test_reviewer_role_argv_is_granular_and_excludes_write_tools",
        ),
    ),
    "REQ-007": (
        ("test_permissions.py", "test_reviewer_argv_no_granular_support"),
        ("test_fallback.py", "test_enter_calls_create_once_and_returns_result"),
        ("test_fallback.py", "test_exit_calls_remove_once_even_when_body_raises"),
        (
            "test_claude_code_adapter.py",
            "test_reviewer_without_granular_support_uses_fallback",
        ),
        (
            "test_codex_adapter.py",
            "test_reviewer_without_granular_support_uses_readonly_worktree",
        ),
    ),
    "REQ-008": (
        ("test_events_translate.py", "test_recognized_line_maps_to_typed_event"),
        (
            "test_claude_code_adapter.py",
            "test_recognized_native_line_emits_typed_event",
        ),
        ("test_codex_adapter.py", "test_recognized_native_line_emits_typed_event"),
    ),
    "REQ-009": (
        ("test_events_translate.py", "test_unrecognized_line_falls_back_to_raw_delta"),
        (
            "test_claude_code_adapter.py",
            "test_unrecognized_native_line_emits_raw_delta",
        ),
        ("test_codex_adapter.py", "test_unrecognized_native_line_emits_raw_delta"),
    ),
    "REQ-010": (
        ("test_no_verification_gates.py", "test_adapters_do_not_import_verifier"),
        (
            "test_no_verification_gates.py",
            "test_adapters_do_not_invoke_gate_binaries_directly",
        ),
    ),
    "REQ-011": (
        (
            "test_claude_code_adapter.py",
            "test_tokens_used_and_session_ref_default_to_none_when_absent",
        ),
        (
            "test_claude_code_adapter.py",
            "test_usage_line_populates_agent_turn_without_polluting_text",
        ),
        (
            "test_codex_adapter.py",
            "test_tokens_used_and_session_ref_default_to_none_when_absent",
        ),
        (
            "test_codex_adapter.py",
            "test_usage_line_populates_agent_turn_without_polluting_text",
        ),
    ),
    "REQ-012": (
        (
            "test_conftest_fakes.py",
            "test_make_fake_process_yields_bytes_lines_wait_and_stderr",
        ),
        ("test_process_seam.py", "test_run_never_touches_real_subprocess_exec"),
        (
            "test_acceptance_w_adapters.py",
            "test_adapter_tests_never_probe_real_cli_binaries",
        ),
        (
            "test_acceptance_w_adapters.py",
            "test_create_subprocess_exec_confined_to_process_module",
        ),
    ),
    "REQ-013": (
        (
            "test_acceptance_w_adapters.py",
            "test_every_adapter_task_has_red_green_evidence",
        ),
    ),
    "REQ-014": (
        (
            "test_acceptance_w_adapters.py",
            "test_adapter_tests_have_no_skip_or_xfail_markers",
        ),
        ("test_acceptance_w_adapters.py", "test_every_requirement_has_a_closing_test"),
    ),
}
"""Traceability [REQ-002]…[REQ-014] → закрывающие тест-кейсы (≥1 на требование).

[REQ-001] отсутствует намеренно: харнесс закрыт до этого воркспейса (TASK-001).
"""


def requirement_node_ids() -> dict[str, tuple[str, ...]]:
    """Матрица traceability в виде готовых node-id pytest."""
    return {
        requirement: tuple(f"{TESTS_DIR}/{module}::{name}" for module, name in pairs)
        for requirement, pairs in REQUIREMENT_TESTS.items()
    }


def repo_root() -> Path:
    """Корень репозитория, выведенный из расположения этого файла (не из CWD)."""
    return Path(__file__).resolve().parents[2]


def adapter_tests_dir() -> Path:
    """Каталог `tests/adapters/`."""
    return Path(__file__).resolve().parent


def adapters_package_dir() -> Path:
    """Каталог пакета `disputatio.adapters` (без зависимости от CWD)."""
    return Path(adapters.__file__).parent


def claims_dir() -> Path:
    """Каталог claim-ов этого воркспейса."""
    return repo_root() / "spec" / ".tdd-evidence" / "claims" / NAMESPACE


def verdicts_dir() -> Path:
    """Каталог вердиктов этого воркспейса."""
    return repo_root() / "spec" / ".tdd-evidence" / "verdicts" / NAMESPACE


def missing_claims(task_ids: Iterable[str]) -> list[str]:
    """Задачи без файла claim — то есть без зафиксированного red-цикла."""
    return [task_id for task_id in task_ids if not _claim_path(task_id).is_file()]


def verdict_statuses(task_ids: Iterable[str]) -> dict[str, str]:
    """`{task_id: verdict}`; `MISSING`, если файла вердикта нет."""
    return {task_id: _verdict_status(task_id) for task_id in task_ids}


def claim_selectors(task_ids: Iterable[str]) -> dict[str, str]:
    """`{task_id: selector}` — какой тест доказывал red каждой задачи."""
    return {
        task_id: json.loads(_claim_path(task_id).read_text(encoding="utf-8"))[
            "selector"
        ]
        for task_id in task_ids
        if _claim_path(task_id).is_file()
    }


def collect_test_node_ids(directory: Path) -> set[str]:
    """Все node-id вида `tests/adapters/test_x.py::test_y` в каталоге.

    Параметризация игнорируется: `pytest` дописывает `[...]` к имени функции,
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


def find_skip_markers(source: str) -> list[str]:
    """Появления `skip`/`skipif`/`xfail`/`importorskip` — маркеры и вызовы.

    Ловит и точечную форму (`pytest.mark.skipif`), и голое имя, втянутое
    `from pytest import skip`: имя вызова раскрывается по импортам модуля.
    """
    tree = ast.parse(source)
    bindings = _import_bindings(tree)
    found = {
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in SKIP_MARK_NAMES
    }
    found |= {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _resolve_call(node, bindings).rpartition(".")[2] in SKIP_MARK_NAMES
    }
    return sorted(found)


def find_binary_probes(source: str) -> list[str]:
    """Вызовы, способные дотянуться до настоящего бинаря ([REQ-012]).

    Сравнивается раскрытое по импортам имя, поэтому `import subprocess as sp`
    и `from subprocess import run` не проходят мимо; в отчёт попадает исходное
    написание вызова.
    """
    tree = ast.parse(source)
    bindings = _import_bindings(tree)
    return sorted(
        {
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _resolve_call(node, bindings) in PROBE_CALLS
        }
    )


def find_subprocess_exec(source: str) -> list[str]:
    """Порождение процесса — вызовы по хвосту имени и сам факт импорта.

    Импорт учитывается отдельно от вызова: `from asyncio import
    create_subprocess_exec` в чужом модуле нарушает [DESIGN-002] и без вызова —
    иначе шов расползается через re-export, а grep-инвариант чек-листа TASK-010
    расходится с исполняемой формой.
    """
    tree = ast.parse(source)
    bindings = _import_bindings(tree)
    found = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in SUBPROCESS_EXEC_NAMES
    }
    found |= {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _resolve_call(node, bindings).rpartition(".")[2] in SUBPROCESS_EXEC_NAMES
    }
    return sorted(found)


def scan_skip_markers(directory: Path) -> dict[str, list[str]]:
    """`{имя тест-модуля: найденные skip/xfail}` — пусто, если чисто."""
    return _scan_tests(directory, find_skip_markers)


def scan_binary_probes(directory: Path) -> dict[str, list[str]]:
    """`{имя тест-модуля: вызовы к реальным процессам}` — пусто, если чисто."""
    return _scan_tests(directory, find_binary_probes)


def scan_subprocess_exec(directory: Path) -> dict[str, list[str]]:
    """`{имя модуля пакета: вызовы create_subprocess_*}` — ожидается только шов."""
    found: dict[str, list[str]] = {}
    for path in sorted(directory.rglob("*.py")):
        calls = find_subprocess_exec(path.read_text(encoding="utf-8"))
        if calls:
            found[path.relative_to(directory).as_posix()] = calls
    return found


def _scan_tests(
    directory: Path, finder: Callable[[str], list[str]]
) -> dict[str, list[str]]:
    """Обходит и `test_*.py`, и вспомогательные модули (`conftest`, сканеры)."""
    found: dict[str, list[str]] = {}
    for path in sorted(directory.glob("*.py")):
        hits = finder(path.read_text(encoding="utf-8"))
        if hits:
            found[path.name] = hits
    return found


def _import_bindings(tree: ast.AST) -> dict[str, str]:
    """`{локальное имя: канонический путь}` — снимает alias'ы импортов модуля."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                separator = "." if module else ""
                bindings[alias.asname or alias.name] = (
                    f"{module}{separator}{alias.name}"
                )
    return bindings


def _resolve_call(node: ast.Call, bindings: dict[str, str]) -> str:
    """Имя вызываемого с раскрытым alias'ом: `sp.run` → `subprocess.run`."""
    called = ast.unparse(node.func)
    head, _, tail = called.partition(".")
    canonical = bindings.get(head)
    if canonical is None:
        return called
    return f"{canonical}.{tail}" if tail else canonical


def _claim_path(task_id: str) -> Path:
    return claims_dir() / f"{task_id}.json"


def _verdict_status(task_id: str) -> str:
    path = verdicts_dir() / f"{task_id}.json"
    if not path.is_file():
        return MISSING_VERDICT
    verdict = json.loads(path.read_text(encoding="utf-8")).get("verdict")
    return verdict if isinstance(verdict, str) else MISSING_VERDICT
