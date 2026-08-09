"""Итоговая приёмка воркстрима `w-context`: TASK-008, [NFR-001]…[NFR-003].

Приёмка ничего не реализует, поэтому её честный red — единственное
состояние, которым она владеет: собственный TDD-claim. На момент
`tdd_gate red` claim TASK-008 ещё не записан, и
`test_every_context_task_has_red_green_evidence` падает `AssertionError`;
после записи claim-а он зеленеет, не тронув ни строки продуктового кода.

Остальные кейсы переводят чек-лист задачи в исполняемую форму:

* чистота пакета — ни I/O, ни времени, ни случайности, включая обходы
  через `__import__`, относительный импорт и `from x import *`;
* границы зависимостей — только stdlib и `disputatio.contracts.*`;
* трассируемость — матрица [REQ-001]…[REQ-011] и [NFR-001]…[NFR-003] с
  проверкой существования каждого названного теста, плюс обратная
  сторона: ни один тест-модуль воркстрима не остался вне матрицы;
* связность evidence — вердикт закрывает СВОЙ claim, а не просто гласит
  `PASS`.

Сканеры и таблицы живут в `acceptance_scan.py` (pytest его не собирает),
чтобы приёмочный модуль остался списком утверждений.
"""

import pytest

from . import acceptance_scan as scan


def test_every_context_task_has_red_green_evidence() -> None:
    """Каждая задача воркстрима прошла TDD-цикл, и цикл доказан на диске.

    Собственный claim TASK-008 требуется наравне с остальными: это и есть
    честный red приёмочной задачи. Вердикт TASK-008 не требуется — гейт
    пишет его после этого прогона.
    """
    assert scan.claims_dir().is_dir(), f"нет каталога claim-ов: {scan.claims_dir()}"

    assert scan.missing_claims(scan.CONTEXT_TASKS) == []
    assert scan.verdict_statuses(scan.VERIFIED_TASKS) == dict.fromkeys(
        scan.VERIFIED_TASKS, scan.PASS_VERDICT
    )

    # Не-вакуумность: claim-ы ссылаются на живые тесты, а не на селекторы,
    # от которых на диске ничего не осталось.
    node_ids = scan.collect_test_node_ids(scan.context_tests_dir())
    selectors = scan.claim_selectors(scan.CONTEXT_TASKS)
    assert set(selectors) == set(scan.CONTEXT_TASKS)
    assert {task for task, sel in selectors.items() if sel not in node_ids} == set()


def test_verdicts_are_linked_to_the_claim_they_close() -> None:
    """Вердикт доказывает red-цикл СВОЕЙ задачи, а не просто гласит `PASS`.

    Строка `PASS` сама по себе ничего не значит: вердикт, вынесенный по
    прошлой ревизии claim-а или по чужому red-коммиту, выглядел бы точно
    так же. Пинится сцепка — `task_id`, `claim_revision`, `red_sha` и
    подтверждённый replay.
    """
    checked: list[str] = []
    defects: list[str] = []
    for task_id in scan.CONTEXT_TASKS:
        verdict = scan.evidence_json("verdicts", task_id)
        if verdict is None:
            # Вердикт приёмочной задачи пишется ПОСЛЕ этого прогона.
            if task_id in scan.VERIFIED_TASKS:
                defects.append(f"{task_id}: нет вердикта")
            continue
        checked.append(task_id)

        if verdict.get("task_id") != task_id:
            defects.append(
                f"{task_id}: вердикт помечен задачей {verdict.get('task_id')!r}"
            )
        outcome = verdict.get("verdict")
        if outcome == scan.PASS_VERDICT:
            defects += scan.pass_verdict_defects(
                task_id, verdict, scan.evidence_json("claims", task_id)
            )
        else:
            defects.append(f"{task_id}: вердикт {outcome!r}")

    assert len(checked) >= len(scan.VERIFIED_TASKS), (
        f"проверено {len(checked)} вердиктов при "
        f"{len(scan.VERIFIED_TASKS)} закрытых задачах"
    )
    assert not defects, "evidence не сцеплено: " + "; ".join(defects)


def test_context_package_stays_free_of_io_and_nondeterminism() -> None:
    """[NFR-001]: пакет — чистая функция от артефактов.

    Ни файловой системы, ни сети, ни часов, ни случайности — и ни одного
    из трёх способов протащить их мимо проверки имён: `__import__`/`open`,
    относительного импорта и `from x import *`.
    """
    assert scan.scan_purity(scan.context_package_dir()) == {}


def test_context_package_depends_only_on_stdlib_and_contracts() -> None:
    """[NFR-001]: зависимости пакета — stdlib плюс `disputatio.contracts.*`.

    Ссылка на `core`/`adapters`/`verifier` развернула бы зависимость в
    обратную сторону: сборка промпта обязана оставаться функцией от
    артефактов, а не от оркестратора.
    """
    assert scan.scan_dependencies(scan.context_package_dir()) == {}


def test_no_typecheck_relaxation_covers_the_context_package() -> None:
    """«`pyrefly check` без ошибок» обязан относиться к коду пакета.

    Точечные послабления в репозитории есть — у byte-locked тестов
    соседних воркстримов. На `src/disputatio/context/**` ни одно из них
    распространяться не должно, иначе критерий приёмки закрывается
    строкой в конфиге.
    """
    assert scan.relaxed_package_modules() == {}


@pytest.mark.parametrize("kind", ["REQ", "NFR"])
def test_coverage_matrix_names_only_live_tests_of_this_workstream(kind: str) -> None:
    """Каждая строка матрицы указывает на существующий тест `tests/context/`.

    Существование проверяется разбором AST, а вложенность — сравнением
    родительского каталога: ссылка вида `../contracts/test_base.py`
    разобралась бы без ошибки и зачла бы требование чужим покрытием.
    """
    coverage = scan.requirement_coverage() if kind == "REQ" else scan.nfr_coverage()
    node_ids = scan.collect_test_node_ids(scan.context_tests_dir())

    assert scan.outside_tests_dir(coverage) == []
    dangling = {
        key: sorted(tests - node_ids)
        for key, tests in scan.node_ids_of(coverage).items()
        if tests - node_ids
    }
    assert dangling == {}


def test_every_requirement_of_the_workstream_is_charted() -> None:
    """[REQ-001]…[REQ-011] и [NFR-001]…[NFR-004] учтены без пропусков.

    Требование без строки в матрице — самая тихая дыра приёмки: полнота
    таблицы никем не проверяется, если её не проверить явно. [NFR-004] в
    spec воркстрима не определён вовсе (`maestro-requirements.md` —
    побайтовая копия `maestro-tasks.md`), поэтому он назван в
    `UNSPECIFIED_NFRS`, а не закрыт придуманным тестом.
    """
    assert tuple(sorted(scan.requirement_coverage())) == scan.REQUIREMENT_IDS
    assert [
        req for req, tests in scan.requirement_coverage().items() if not tests
    ] == []

    charted_nfrs = set(scan.nfr_coverage())
    assert charted_nfrs.isdisjoint(scan.UNSPECIFIED_NFRS)
    assert tuple(sorted(charted_nfrs | set(scan.UNSPECIFIED_NFRS))) == scan.NFR_IDS
    assert [nfr for nfr, tests in scan.nfr_coverage().items() if not tests] == []


def test_no_test_module_is_orphaned_from_the_coverage_matrix() -> None:
    """Обратная сторона матрицы: тест-модуль без требования — не покрытие.

    Файлы, появившиеся как review-фикс, не попадают ни в один claim; без
    этой проверки они молча выпадают и из матрицы, и приёмка отчитывается
    о полноте, которой нет.
    """
    on_disk = scan.test_modules_on_disk(scan.context_tests_dir())
    known = (
        scan.charted_modules()
        | scan.claimed_modules(scan.CONTEXT_TASKS)
        | scan.EXEMPT_TEST_MODULES
    )

    assert on_disk, "в tests/context/ не найдено ни одного тест-модуля"
    assert sorted(on_disk - known) == []


def test_find_purity_offences_detects_every_bypass() -> None:
    """Самотест сканера: прямой импорт, обход, относительный и звёздный.

    Без него `scan_purity == {}` доказывает лишь, что сканер молчит.
    """
    source = (
        "import os.path as p\n"
        "from datetime import datetime\n"
        "from . import sections\n"
        "from disputatio.contracts.review import *\n\n\n"
        "def read() -> str:\n"
        "    module = __import__('pathlib')\n"
        "    return open('/etc/hosts').read() + str(module) + str(p) + str(datetime)\n"
    )

    assert scan.find_purity_offences(source) == [
        "import datetime",
        "import os.path",
        "relative import .",
        "star import disputatio.contracts.review",
        "call __import__",
        "call open",
    ]


def test_find_purity_offences_allows_a_clean_module() -> None:
    """Обратная сторона самотеста: настоящий модуль пакета не ловится."""
    source = (
        "from typing import Final\n\n"
        "from disputatio.contracts.review import Issue\n\n"
        "TITLE: Final = '## Замечания'\n\n\n"
        "def render(issues: list[Issue]) -> str:\n"
        "    return TITLE if issues else ''\n"
    )

    assert scan.find_purity_offences(source) == []


def test_find_dependency_violations_flags_third_party_and_sibling_packages() -> None:
    """Граница зависимостей: `pydantic` и соседний пакет — оба нарушения."""
    source = (
        "import pydantic\n"
        "from disputatio.core.machine import Machine\n"
        "from disputatio.contracts.review import Issue\n"
        "from collections.abc import Sequence\n"
    )

    assert scan.find_dependency_violations(source) == [
        "disputatio.core.machine",
        "pydantic",
    ]
