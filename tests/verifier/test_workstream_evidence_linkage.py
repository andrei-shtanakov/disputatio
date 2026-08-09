"""Связность evidence workstream'а (review-фикс TASK-011, NFR-002).

`test_workstream_evidence_is_complete` считает задачу закрытой по факту
наличия файлов и строки `PASS`/`WAIVED` в вердикте. Сам вердикт при этом
ни с чем не сверяется: `task_id`, `claim_revision`, `red_sha`,
`red_replay` и `selector_at_head` не читаются вовсе. Значит устаревший
вердикт (вынесенный по предыдущей ревизии claim'а) и вердикт, в котором
red-цикл не подтверждён, проходят приёмку наравне с честными — NFR-002
(«каждая leaf-задача — TDD-цикл red → green») держится на литерале, а не
на доказательстве.

Здесь пинится именно связность: вердикт обязан ссылаться на СВОЙ claim
той же ревизии и тем же red-коммитом, а `WAIVED` — на waiver, одобренный
человеком.

Вторым тестом закрывается граница §6.1: матрица покрытия обязана
называть модули самого `tests/verifier/**`. `_test_names` склеивает имя
модуля с каталогом без проверки вложенности, и ссылка вида
`../contracts/test_base.py` удовлетворила бы матрицу, не покрыв
требование ни одним тестом workstream'а.

Константы и матрица берутся из приёмочного модуля, а не копируются: их
переименование обязано валить приёмку, а не тихо превращать эти тесты в
проверку собственной копии.
"""

from typing import Any, Final

from .test_verifier_port import (
    _ACCEPTANCE_TASK,
    _CLOSED_TASKS,
    _REQUIREMENT_COVERAGE,
    _TESTS_DIR,
    _evidence_json,
)

# Вердикт приёмочной задачи гейт переписывает ПОСЛЕ прогона suite, поэтому
# его наличие не требуется — но если файл на диске есть, он проверяется
# наравне с остальными.
_OPTIONAL_TASKS: Final = (_ACCEPTANCE_TASK,)

_HONEST_RED_REPLAY: Final = "EXPECTED_FAIL"
_HONEST_SELECTOR_AT_HEAD: Final = "PASS"


def _pass_verdict_defects(
    task_id: str, verdict: dict[str, Any], claim: dict[str, Any] | None
) -> list[str]:
    """Расхождения `PASS`-вердикта со своим claim'ом; пустой список — чисто."""
    if claim is None:
        return [f"{task_id}: вердикт PASS без claim'а"]

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
        defects.append(f"{task_id}: red_sha вердикта не совпадает с claim'ом")
    if verdict.get("red_replay") != _HONEST_RED_REPLAY:
        defects.append(
            f"{task_id}: red_replay {verdict.get('red_replay')!r}, "
            f"ожидался {_HONEST_RED_REPLAY!r}"
        )
    if verdict.get("selector_at_head") != _HONEST_SELECTOR_AT_HEAD:
        defects.append(
            f"{task_id}: selector_at_head {verdict.get('selector_at_head')!r}, "
            f"ожидался {_HONEST_SELECTOR_AT_HEAD!r}"
        )
    return defects


def _waived_verdict_defects(task_id: str) -> list[str]:
    """Расхождения `WAIVED`-вердикта с waiver'ом; пустой список — чисто."""
    waiver = _evidence_json("waivers", task_id)
    if waiver is None:
        return [f"{task_id}: вердикт WAIVED без waiver'а"]
    if waiver.get("approved_by") != "human":
        return [
            f"{task_id}: waiver одобрен {waiver.get('approved_by')!r}, не человеком"
        ]
    return []


def test_verdicts_are_linked_to_the_claim_they_close() -> None:
    """NFR-002: вердикт доказывает red-цикл СВОЕЙ задачи, а не просто гласит PASS.

    Проверяется весь workstream разом: `task_id` внутри файла совпадает с
    именем файла (скопированный вердикт соседа не закрывает задачу),
    `PASS` сцеплен с claim'ом по ревизии и red-коммиту и подтверждён
    честным replay'ем, `WAIVED` — waiver'ом человека.
    """
    checked: list[str] = []
    defects: list[str] = []
    for task_id in (*_CLOSED_TASKS, *_OPTIONAL_TASKS):
        verdict = _evidence_json("verdicts", task_id)
        if verdict is None:
            if task_id not in _OPTIONAL_TASKS:
                defects.append(f"{task_id}: нет вердикта")
            continue
        checked.append(task_id)

        if verdict.get("task_id") != task_id:
            defects.append(
                f"{task_id}: вердикт помечен задачей {verdict.get('task_id')!r}"
            )
        outcome = verdict.get("verdict")
        if outcome == "PASS":
            defects += _pass_verdict_defects(
                task_id, verdict, _evidence_json("claims", task_id)
            )
        elif outcome == "WAIVED":
            defects += _waived_verdict_defects(task_id)
        else:
            defects.append(f"{task_id}: вердикт {outcome!r}")

    # Пустой цикл прошёл бы молча: приёмка обязана видеть весь workstream.
    assert len(checked) >= len(_CLOSED_TASKS), (
        f"проверено {len(checked)} вердиктов при {len(_CLOSED_TASKS)} закрытых задачах"
    )
    assert not defects, "evidence не сцеплено: " + "; ".join(defects)


def test_coverage_matrix_names_only_modules_of_the_verifier_suite() -> None:
    """§6.1: покрывающие тесты живут в `tests/verifier/**`, а не где угодно.

    `_test_names` склеивает имя модуля с каталогом без проверки
    вложенности, поэтому матрица, сославшаяся на тест соседнего
    workstream'а, разобралась бы без ошибки и зачла требование чужим
    покрытием.
    """
    outside: list[str] = []
    for req, pairs in sorted(_REQUIREMENT_COVERAGE.items()):
        for module, _test_name in pairs:
            path = (_TESTS_DIR / module).resolve()
            if path.parent != _TESTS_DIR or not path.name.startswith("test_"):
                outside.append(f"{req} → {module}")

    assert not outside, "матрица ссылается вне tests/verifier/: " + ", ".join(outside)
