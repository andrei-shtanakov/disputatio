"""Приёмка workstream'а `w-runtime`: чистота, качество, трассируемость (§5.4).

[TASK-026], [DESIGN-026]. Пять обязательных проверок §5.4 (`pytest -q`,
`ruff check .`, `pyrefly check`, `tdd_gate audit`, `git diff --stat
pilot/wave-1..HEAD`) выполняются командами и фиксируются их выводом. Здесь
живёт то, что разовым прогоном команды не удержишь и что обязано оставаться
верным при каждом прогоне набора:

* матрица REQ → тест-модуль полна и не называет вымышленных модулей;
* ни один тест-модуль workstream'а не осиротел от матрицы — файлы, рождённые
  ревью-фиксами, claim'а не имеют и иначе не видны вообще ничему;
* граница INV-11 не размылась: `disputatio.core` по-прежнему чист;
* runtime и CLI ходят в пакеты волн 0–1 только через публичные `__init__`;
* модули волн 0–1 не редактировались — а если редактировались, то ровно там,
  где коммит несёт операторскую санкцию `Operator-Scope-Exception`;
* evidence-цепочка задач 001…026 сомкнута: claim ↔ verdict по ревизии и
  `red_sha`, а селектор claim'а резолвится в реально существующий тест.

Известные отклонения не прячутся, а перечислены явными константами с
основанием: санкционированная правка `core/deciding.py` (DISP-DEF-001) и
предписанный чек-листом [TASK-011] импорт `context.tags`. Любое НОВОЕ
отклонение валит набор.
"""

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Final

import pytest

from disputatio.runtime import scan_package_purity

#: Корень репозитория: tests/runtime/<файл> → parents[2].
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Каталог тестов; пути матрицы задаются относительно него.
TESTS_DIR: Final[Path] = REPO_ROOT / "tests"

#: Каталоги, все тест-модули которых обязаны присутствовать в матрице.
#: `integration` добавлен вместе со сквозными сценариями пайплайна (SPEC-002
#: §10): набор, не попавший ни в один детектор, не виден ничему — а именно он
#: и доказывает, что собранные по кускам пакеты работают вместе.
CHARTED_DIRS: Final[tuple[str, ...]] = ("runtime", "cli", "integration")

#: База workstream'а — состояние репозитория до его первой задачи.
BASE_REV: Final[str] = "pilot/wave-1"

#: Матрица трассируемости [DESIGN-026]. Пары, а не node-id-строки: склейка
#: `"tests/x.py" "::test_y"` внутри коллекции — implicit concatenation (ISC004).
REQUIREMENT_COVERAGE: Final[tuple[tuple[str, str], ...]] = (
    ("REQ-001", "runtime/test_composition.py"),
    ("REQ-001", "runtime/test_composition_adapter_errors.py"),
    ("REQ-002", "runtime/test_core_purity.py"),
    ("REQ-002", "runtime/test_core_purity_aliases.py"),
    ("REQ-003", "runtime/test_step_proposing.py"),
    ("REQ-003", "runtime/test_step_proposing_artifact_fidelity.py"),
    ("REQ-004", "runtime/test_step_verifying.py"),
    ("REQ-004", "runtime/test_step_verifying_pairing.py"),
    ("REQ-005", "runtime/test_step_reviewing.py"),
    ("REQ-005", "runtime/test_review_json_extraction.py"),
    ("REQ-006", "runtime/test_schema_retry.py"),
    ("REQ-006", "runtime/test_schema_retry_wiring.py"),
    ("REQ-007", "runtime/test_step_deciding.py"),
    ("REQ-007", "runtime/test_step_deciding_resume.py"),
    ("REQ-007", "runtime/test_decide_open_issues_cases.py"),
    ("REQ-007", "runtime/test_carry_chain.py"),
    ("REQ-008", "runtime/test_write_ahead.py"),
    ("REQ-008", "runtime/test_write_ahead_final_state.py"),
    ("REQ-009", "runtime/test_budget.py"),
    ("REQ-009", "cli/test_cli_run_budget_outcome.py"),
    ("REQ-010", "runtime/test_git_preflight.py"),
    ("REQ-010", "runtime/test_git_preflight_hardening.py"),
    ("REQ-011", "runtime/test_git_commit_round.py"),
    ("REQ-011", "runtime/test_git_round_cycle.py"),
    ("REQ-012", "runtime/test_git_reset.py"),
    ("REQ-012", "runtime/test_git_reset_ambiguity.py"),
    ("REQ-012", "runtime/test_git_reset_hardening.py"),
    ("REQ-012", "runtime/test_git_reset_hostile_inputs.py"),
    ("REQ-013", "runtime/test_changes_patch.py"),
    ("REQ-013", "runtime/test_changes_patch_hardening.py"),
    ("REQ-014", "runtime/test_resume_dispatch.py"),
    ("REQ-014", "runtime/test_config_snapshot_reader.py"),
    ("REQ-014", "runtime/test_config_snapshot_defaults.py"),
    ("REQ-015", "runtime/test_step_idempotency.py"),
    ("REQ-015", "runtime/test_step_replay_hygiene.py"),
    ("REQ-016", "runtime/test_resume_append_only.py"),
    ("REQ-016", "runtime/test_append_only_scan_evasions.py"),
    ("REQ-017", "runtime/test_export_converged.py"),
    ("REQ-017", "runtime/test_export_outcome_fidelity.py"),
    ("REQ-018", "runtime/test_export_partial.py"),
    ("REQ-019", "cli/test_cli_run.py"),
    ("REQ-019", "cli/test_cli_run_wiring.py"),
    ("REQ-020", "cli/test_cli_resume.py"),
    ("REQ-020", "cli/test_cli_resume_wiring.py"),
    ("REQ-021", "cli/test_entrypoint.py"),
    ("REQ-022", "runtime/test_package_surface.py"),
    ("REQ-022", "runtime/test_package_surface_star_names.py"),
    ("REQ-023", "runtime/test_conftest_fixtures.py"),
    ("REQ-023", "runtime/test_conftest_hermeticity_and_fakes.py"),
    ("REQ-024", "runtime/test_e2e_converged.py"),
    ("REQ-025", "runtime/test_e2e_escalation.py"),
)

#: Сама приёмка трассируется в §5.4, а не в REQ: без этой пары она была бы
#: осиротевшим модулем в собственном детекторе.
ACCEPTANCE_COVERAGE: Final[tuple[tuple[str, str], ...]] = (
    ("§5.4", "runtime/test_acceptance.py"),
)

#: Расширения цикла из SPEC-002: точки опроса политик границы раунда и
#: жизненного цикла хода автора, операции порта под операторские решения.
#: Отдельной парой, а не строкой в `REQUIREMENT_COVERAGE`: REQ-001…REQ-025 —
#: требования SPEC-001, и приписать им чужое покрытие значило бы утверждать
#: про требование то, чего оно не говорит. Трассировка — §7.1 и инварианты
#: P4/P6/P9, для adoption/reconciliation — §3.1, §7.3 и §8.1.
PIPELINE_COVERAGE: Final[tuple[tuple[str, str], ...]] = (
    ("SPEC-002 §7.1", "runtime/test_round_boundary.py"),
    ("SPEC-002 §7.1", "runtime/test_lifecycle_policy.py"),
    ("SPEC-002 §3.1", "runtime/test_git_adoption.py"),
    ("SPEC-002 §3.1", "runtime/test_pipeline_config.py"),
    ("SPEC-002 §8.2", "runtime/test_pipeline_export.py"),
    ("SPEC-002 §4.3", "runtime/test_pipeline_runner.py"),
    ("SPEC-002 §8.1", "runtime/test_pipeline_resume.py"),
    ("SPEC-002 §2 P9", "runtime/test_pipeline_integrity.py"),
    ("SPEC-002 §3.1", "runtime/test_pipeline_adopt.py"),
    ("SPEC-002 §3.2", "runtime/test_pipeline_config_kinds.py"),
    ("SPEC-002 §7.1", "runtime/test_pipeline_runner_document.py"),
    ("SPEC-002 §2 P0", "runtime/test_document_composition.py"),
    ("SPEC-002 §8.2", "runtime/test_export_document.py"),
    ("SPEC-002 §3.1", "cli/test_cli_pipeline_kind.py"),
    ("SPEC-002 §10", "integration/test_pipeline_e2e.py"),
    ("SPEC-002 §10", "integration/test_document_pipeline_e2e.py"),
    ("SPEC-002 §4.2", "runtime/test_pipeline_semantic_proof.py"),
)

#: Полная матрица.
COVERAGE: Final[tuple[tuple[str, str], ...]] = (
    REQUIREMENT_COVERAGE + ACCEPTANCE_COVERAGE + PIPELINE_COVERAGE
)

#: Требования workstream'а по спецификации: REQ-001…REQ-025.
ALL_REQUIREMENTS: Final[tuple[str, ...]] = tuple(
    f"REQ-{number:03d}" for number in range(1, 26)
)

#: Каталоги, целиком принадлежащие workstream'у (§4.3).
SCOPE_DIR_PREFIXES: Final[tuple[str, ...]] = (
    "src/disputatio/runtime/",
    "tests/runtime/",
    "tests/cli/",
)

#: Отдельные файлы scope'а.
SCOPE_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "src/disputatio/__init__.py",
    "src/disputatio/cli.py",
    "tests/conftest.py",
)

#: `spec/.tdd-evidence/*/ws-w-runtime/**` — claims, verdicts, waivers своего ws.
EVIDENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"spec/\.tdd-evidence/[^/]+/ws-w-runtime/.+"
)

#: Пакеты волн 0–1: чужой scope, редактировать который задачам запрещено.
WAVE_PACKAGES: Final[tuple[str, ...]] = (
    "adapters",
    "context",
    "contracts",
    "core",
    "events",
    "verifier",
)

#: Трейлер, которым оператор санкционирует выход за §4.3.
SCOPE_EXCEPTION_TRAILER: Final[str] = "Operator-Scope-Exception:"

#: Единственный предписанный импорт подмодуля волн 0–1: чек-лист [TASK-011]
#: требует передавать текст ошибки агенту именно `context.tags`
#: (`wrap_artifact_data`), и это же пинит байт-locked `test_schema_retry.py`.
#: Публичный `disputatio.context.__all__` функцию не отдаёт — снять отклонение
#: может только w-context, экспортировав её.
SANCTIONED_SUBMODULE_IMPORTS: Final[tuple[tuple[str, str], ...]] = (
    ("runtime/retry.py", "disputatio.context.tags"),
)

#: Корень evidence и имя workstream'а в нём.
EVIDENCE_DIR: Final[Path] = REPO_ROOT / "spec" / ".tdd-evidence"
WORKSTREAM: Final[str] = "ws-w-runtime"

#: Задачи декомпозиции — вместе с самой приёмкой.
TASK_IDS: Final[tuple[str, ...]] = tuple(
    f"TASK-{number:03d}" for number in range(1, 27)
)

#: Verdict приёмочной задачи гейт пишет ПОСЛЕ прогона набора — её вердикт
#: обязательным быть не может, claim обязан существовать.
ACCEPTANCE_TASK: Final[str] = "TASK-026"

#: Вердикты, засчитываемые как честно закрытый TDD-цикл.
ACCEPTED_VERDICTS: Final[frozenset[str]] = frozenset({"PASS", "WAIVED"})


def _git(*args: str) -> str:
    """Читающая git-команда в корне репозитория."""
    completed = subprocess.run(
        ("git", *args),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _base_rev_available() -> bool:
    """Существует ли ещё база workstream'а в этом репозитории.

    После интеграции ветка `pilot/wave-1` удаляется по правилу полирепо, и
    проверки границ scope теряют базу для сравнения — не потому, что границы
    нарушены, а потому, что сравнивать больше не с чем. Раньше это давало
    вечно красный `CalledProcessError` на `master`: приёмочный тест
    workstream'а пережил сам workstream и требовал ссылку, которой нет.

    `OSError` перехватывается наравне с `CalledProcessError` (Copilot, PR #15):
    функция вызывается на импорте модуля, поэтому отсутствующий `git` уронил бы
    не проверку, а сбор тестов целиком. «git недоступен» — это тоже «базы нет».
    """
    try:
        subprocess.run(
            ("git", "rev-parse", "--verify", f"{BASE_REV}^{{commit}}"),
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


requires_base_rev = pytest.mark.skipif(
    not _base_rev_available(),
    reason=(
        f"база workstream'а {BASE_REV!r} удалена после интеграции — "
        "границы scope проверять не с чем"
    ),
)


def _changed_paths() -> tuple[str, ...]:
    """Пути, изменённые workstream'ом относительно базы."""
    output = _git("diff", "--name-only", f"{BASE_REV}..HEAD")
    return tuple(line for line in output.splitlines() if line)


def _authorized_exceptions() -> frozenset[str]:
    """Пути, на которые в истории workstream'а выдана операторская санкция."""
    body = _git("log", "--format=%B", f"{BASE_REV}..HEAD")
    return frozenset(
        line.split(":", 1)[1].strip()
        for line in body.splitlines()
        if line.startswith(SCOPE_EXCEPTION_TRAILER)
    )


def _is_in_scope(path: str) -> bool:
    """Принадлежит ли путь объявленному scope'у workstream'а (§4.3)."""
    return (
        path in SCOPE_FILES
        or path.startswith(SCOPE_DIR_PREFIXES)
        or EVIDENCE_PATTERN.fullmatch(path) is not None
    )


def _charted_modules() -> frozenset[str]:
    """Модули, заявленные матрицей."""
    return frozenset(module for _, module in COVERAGE)


def _on_disk_modules() -> frozenset[str]:
    """Тест-модули workstream'а на диске, путями от `tests/`."""
    found: set[str] = set()
    for directory in CHARTED_DIRS:
        for module in (TESTS_DIR / directory).rglob("test_*.py"):
            found.add(module.relative_to(TESTS_DIR).as_posix())
    return frozenset(found)


def _package_of(source: Path) -> str:
    """Пакет модуля — база для разрешения относительных импортов."""
    parts = source.relative_to(REPO_ROOT / "src").with_suffix("").parts
    return ".".join(parts[:-1])


def _resolve_import(module: str | None, level: int, package: str) -> str:
    """Абсолютное имя импорта; `level > 0` разрешается относительно пакета."""
    if level == 0:
        return module or ""
    base = package.split(".")
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join([*base, module]) if module else ".".join(base)


def _is_submodule(name: str, roots: tuple[str, ...]) -> bool:
    """Указывает ли имя внутрь пакета волн 0–1 глубже его `__init__`."""
    return any(name.startswith(f"{root}.") for root in roots)


def _scanned_sources() -> tuple[Path, ...]:
    """Модули, чьи импорты обязаны ходить только в публичные `__init__`."""
    package_dir = REPO_ROOT / "src" / "disputatio"
    runtime_modules = sorted((package_dir / "runtime").rglob("*.py"))
    return (*runtime_modules, package_dir / "cli.py")


def _submodule_imports() -> tuple[tuple[str, str], ...]:
    """Импорты подмодулей пакетов волн 0–1 из `runtime/**` и `cli.py`."""
    package_dir = REPO_ROOT / "src" / "disputatio"
    wave_roots = tuple(f"disputatio.{name}" for name in WAVE_PACKAGES)
    found: set[tuple[str, str]] = set()
    for source in _scanned_sources():
        where = source.relative_to(package_dir).as_posix()
        package = _package_of(source)
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_submodule(alias.name, wave_roots):
                        found.add((where, alias.name))
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            resolved = _resolve_import(node.module, node.level, package)
            if _is_submodule(resolved, wave_roots):
                found.add((where, resolved))
                continue
            if resolved not in wave_roots:
                continue
            # Публичный пакет назван правильно, но имя может оказаться его
            # подмодулем — тот же обход границы, только через `import from`.
            # Имена сверяются со списком каталога, а не `Path.is_file()`:
            # на регистронезависимой ФС `contracts/Decision.py` «существует».
            imported_dir = package_dir / resolved.split(".")[-1]
            submodules = {module.stem for module in imported_dir.glob("*.py")}
            for alias in node.names:
                if alias.name in submodules:
                    found.add((where, f"{resolved}.{alias.name}"))
    return tuple(sorted(found))


def _evidence(kind: str, task_id: str) -> dict[str, object] | None:
    """Запись evidence workstream'а или `None`, если её нет."""
    path = EVIDENCE_DIR / kind / WORKSTREAM / f"{task_id}.json"
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} — не объект JSON"
    return loaded


def _defined_test_names(module: Path) -> frozenset[str]:
    """Имена тест-функций модуля (включая объявленные внутри классов)."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def test_every_requirement_is_covered_by_a_test_module() -> None:
    """Каждый REQ-001…REQ-025 назван матрицей хотя бы одним модулем (§5.4)."""
    charted = {requirement for requirement, _ in REQUIREMENT_COVERAGE}
    missing = sorted(set(ALL_REQUIREMENTS) - charted)

    assert not missing, f"требования без тест-модуля в матрице: {missing}"


def test_matrix_names_no_requirement_outside_the_specification() -> None:
    """Матрица не покрывает вымышленных требований — иначе полнота вакуумна."""
    charted = {requirement for requirement, _ in REQUIREMENT_COVERAGE}
    unknown = sorted(charted - set(ALL_REQUIREMENTS))

    assert not unknown, f"матрица называет требования вне спецификации: {unknown}"


def test_charted_modules_exist_inside_the_workstream_test_dirs() -> None:
    """Каждый заявленный модуль существует и лежит внутри своих каталогов."""
    allowed = tuple((TESTS_DIR / directory).resolve() for directory in CHARTED_DIRS)
    missing: list[str] = []
    outside: list[str] = []
    for module in sorted(_charted_modules()):
        path = (TESTS_DIR / module).resolve()
        if not path.is_file():
            missing.append(module)
            continue
        if not any(path.is_relative_to(directory) for directory in allowed):
            outside.append(module)

    assert not missing, f"матрица ссылается на несуществующие модули: {missing}"
    assert not outside, f"модули вне tests/runtime и tests/cli: {outside}"


def test_no_test_module_is_orphaned_from_the_matrix() -> None:
    """Каждый тест-модуль на диске присутствует в матрице.

    Файлы, рождённые ревью-фиксами, claim'ов не имеют, и ничто, кроме этой
    проверки, об их существовании не знает.
    """
    orphans = sorted(_on_disk_modules() - _charted_modules())

    assert not orphans, f"тест-модули вне матрицы трассируемости: {orphans}"


def test_core_package_is_still_pure() -> None:
    """INV-11 не размылся за workstream: ядро не импортирует реализаций."""
    violations = scan_package_purity(
        REPO_ROOT / "src" / "disputatio" / "core",
        package_name="disputatio.core",
    )

    assert violations == [], f"нарушения INV-11 в ядре: {violations}"


def test_runtime_and_cli_import_wave_packages_only_through_public_init() -> None:
    """Импорты волн 0–1 идут в публичный `__init__`, кроме санкционированных."""
    found = _submodule_imports()

    assert found == SANCTIONED_SUBMODULE_IMPORTS, (
        f"импорты подмодулей волн 0–1: {list(found)}; "
        f"разрешены только {list(SANCTIONED_SUBMODULE_IMPORTS)}"
    )


@requires_base_rev
def test_wave_packages_are_edited_only_under_operator_sanction() -> None:
    """Правки в чужих пакетах существуют только там, где есть санкция."""
    wave_prefixes = tuple(f"src/disputatio/{name}/" for name in WAVE_PACKAGES)
    edited = [path for path in _changed_paths() if path.startswith(wave_prefixes)]
    authorized = _authorized_exceptions()
    unsanctioned = sorted(path for path in edited if path not in authorized)

    assert not unsanctioned, (
        f"модули волн 0–1 изменены без трейлера "
        f"{SCOPE_EXCEPTION_TRAILER!r}: {unsanctioned}"
    )


@requires_base_rev
def test_workstream_diff_stays_inside_the_declared_scope() -> None:
    """`git diff pilot/wave-1..HEAD` не выходит за §4.3 без санкции."""
    authorized = _authorized_exceptions()
    strayed = sorted(
        path
        for path in _changed_paths()
        if not _is_in_scope(path) and path not in authorized
    )

    assert not strayed, f"файлы вне scope workstream'а: {strayed}"


def test_workstream_evidence_is_complete() -> None:
    """Цепочка claim ↔ verdict сомкнута для задач 001…026.

    Вердикт проверяется не наличием слова PASS, а сцепкой: ревизия claim'а,
    `red_sha`, `red_replay == EXPECTED_FAIL`, `selector_at_head == PASS`.
    Вердикт самой приёмки гейт пишет после прогона набора — он необязателен.
    """
    without_claim: list[str] = []
    broken: list[str] = []
    for task_id in TASK_IDS:
        claim = _evidence("claims", task_id)
        if claim is None:
            without_claim.append(task_id)
            continue
        verdict = _evidence("verdicts", task_id)
        if verdict is None:
            if task_id != ACCEPTANCE_TASK:
                broken.append(f"{task_id}: нет вердикта")
            continue
        if verdict.get("verdict") not in ACCEPTED_VERDICTS:
            broken.append(f"{task_id}: вердикт {verdict.get('verdict')!r}")
        if verdict.get("claim_revision") != claim.get("revision"):
            broken.append(f"{task_id}: вердикт по чужой ревизии claim'а")
        if verdict.get("red_sha") != claim.get("red_sha"):
            broken.append(f"{task_id}: вердикт по чужому red_sha")
        if verdict.get("red_replay") != "EXPECTED_FAIL":
            broken.append(f"{task_id}: red не воспроизведён")
        if verdict.get("selector_at_head") != "PASS":
            broken.append(f"{task_id}: селектор не зелёный на HEAD")

    assert not without_claim, f"задачи без claim-ов: {without_claim}"
    assert not broken, f"разорванная evidence-цепочка: {broken}"


def test_every_claim_selector_resolves_to_a_real_test() -> None:
    """Селектор каждого claim'а указывает на существующий тест workstream'а."""
    unresolved: list[str] = []
    for task_id in TASK_IDS:
        claim = _evidence("claims", task_id)
        if claim is None:
            continue
        selector = claim.get("selector")
        assert isinstance(selector, str), f"{task_id}: селектор не строка"
        module_path, _, test_name = selector.partition("::")
        module = (REPO_ROOT / module_path).resolve()
        if not module.is_file():
            unresolved.append(f"{task_id}: нет модуля {module_path}")
            continue
        if test_name.split("[")[0] not in _defined_test_names(module):
            unresolved.append(f"{task_id}: нет теста {selector}")

    assert not unresolved, f"селекторы claim-ов не резолвятся: {unresolved}"
