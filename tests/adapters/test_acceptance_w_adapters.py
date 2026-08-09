"""Итоговая приёмка воркспейса `w-adapters`: TASK-010, [REQ-013], [REQ-014].

Задача-приёмка ничего не реализует, поэтому её честный red — единственное
отслеживаемое состояние, которым она владеет: собственный TDD-claim. На момент
`tdd_gate red` claim TASK-010 ещё не записан, и
`test_every_adapter_task_has_red_green_evidence` падает `AssertionError`;
после записи claim он зеленеет без единой правки продуктового кода.

Остальные кейсы переводят чек-лист задачи в исполняемую форму: матрица
traceability [REQ-002]…[REQ-014], отсутствие `skip`/`xfail`, отсутствие
обращений к настоящим `claude`/`codex` и локализация
`asyncio.create_subprocess_exec` в `_process.py`. Сканеры вынесены в
`acceptance_scan.py` (не собирается pytest-ом) по образцу `gate_scan.py`.
"""

from . import acceptance_scan as scan

REQUIREMENT_IDS = tuple(f"REQ-{number:03d}" for number in range(2, 15))
"""[REQ-002]…[REQ-014] — все требования, которые закрывает этот воркспейс."""


def test_every_adapter_task_has_red_green_evidence() -> None:
    """[REQ-013]: у TASK-001…010 есть claim, у закрытых задач — вердикт `PASS`."""
    assert scan.claims_dir().is_dir()

    assert scan.missing_claims(scan.ADAPTER_TASKS) == []
    assert scan.verdict_statuses(scan.VERIFIED_TASKS) == dict.fromkeys(
        scan.VERIFIED_TASKS, scan.PASS_VERDICT
    )

    # Не-вакуумность: claim-ы ссылаются на живые тесты, а не на пустые селекторы.
    node_ids = scan.collect_test_node_ids(scan.adapter_tests_dir())
    selectors = scan.claim_selectors(scan.ADAPTER_TASKS)
    assert set(selectors) == set(scan.ADAPTER_TASKS)
    assert {
        task_id for task_id, sel in selectors.items() if sel not in node_ids
    } == set()


def test_every_requirement_has_a_closing_test() -> None:
    """[REQ-014]: каждое требование [REQ-002]…[REQ-014] закрыто ≥1 тест-кейсом."""
    node_ids = scan.collect_test_node_ids(scan.adapter_tests_dir())
    matrix = scan.requirement_node_ids()

    assert tuple(sorted(matrix)) == REQUIREMENT_IDS
    assert [req for req, tests in matrix.items() if not tests] == []

    dangling = {
        req: sorted(set(tests) - node_ids)
        for req, tests in matrix.items()
        if set(tests) - node_ids
    }
    assert dangling == {}


def test_adapter_tests_have_no_skip_or_xfail_markers() -> None:
    """[REQ-014]: приёмочный прогон зелёный целиком — без пропусков и `xfail`."""
    assert scan.scan_skip_markers(scan.adapter_tests_dir()) == {}


def test_adapter_tests_never_probe_real_cli_binaries() -> None:
    """[REQ-012]: ни один тест не ищет и не запускает настоящие `claude`/`codex`."""
    assert scan.scan_binary_probes(scan.adapter_tests_dir()) == {}


def test_create_subprocess_exec_confined_to_process_module() -> None:
    """[REQ-012]/[DESIGN-002]: порождение процесса — только в шве `_process.py`."""
    assert scan.scan_subprocess_exec(scan.adapters_package_dir()) == {
        scan.PROCESS_SEAM_MODULE: ["asyncio.create_subprocess_exec"]
    }


def test_find_skip_markers_detects_decorators_and_calls() -> None:
    """Негативный самотест сканера: и маркер-декоратор, и императивный пропуск."""
    source = (
        "import pytest\n\n\n"
        "@pytest.mark.skipif(True, reason='нет бинаря')\n"
        "def test_one() -> None:\n"
        "    pytest.skip('позже')\n"
    )

    assert scan.find_skip_markers(source) == ["pytest.mark.skipif", "pytest.skip"]


def test_find_skip_markers_allows_ordinary_test_module() -> None:
    """Обычный тест без пропусков не ловится (иначе сканер вакуумен наоборот)."""
    source = "def test_one() -> None:\n    assert True\n"

    assert scan.find_skip_markers(source) == []


def test_find_binary_probes_detects_which_and_direct_launch() -> None:
    """`shutil.which` и прямой запуск процесса — оба нарушают [REQ-012]."""
    source = (
        "import shutil\n"
        "import subprocess\n\n\n"
        "def test_one() -> None:\n"
        "    if shutil.which('claude') is None:\n"
        "        return\n"
        "    subprocess.run(['claude', '-p', 'hi'])\n"
    )

    assert scan.find_binary_probes(source) == ["shutil.which", "subprocess.run"]


def test_find_binary_probes_allows_fake_process_and_patched_names() -> None:
    """Фейк из conftest и `setattr` по строковому имени — легальный паттерн."""
    source = (
        "def test_one(monkeypatch, make_fake_process) -> None:\n"
        "    monkeypatch.setattr(asyncio, 'create_subprocess_exec', _raise)\n"
        "    process = make_fake_process(stdout_lines=[b'hi\\n'])\n"
        "    adapter.run('prompt')\n"
    )

    assert scan.find_binary_probes(source) == []


def test_find_subprocess_exec_matches_plain_import_form() -> None:
    """Шов ловится и без префикса `asyncio.` — по хвосту имени вызова."""
    source = (
        "from asyncio import create_subprocess_exec\n\n\n"
        "async def launch() -> None:\n"
        "    await create_subprocess_exec('claude')\n"
    )

    assert scan.find_subprocess_exec(source) == ["create_subprocess_exec"]
