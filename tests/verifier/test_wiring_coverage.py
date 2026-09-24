"""Покрытие и находки гейта wiring: сборка отчёта и вывод (design §5.2, §5.3, §6).

`check_wiring` — точка сборки всего гейта поверх уже готовых кирпичей задач
2-4 (`parse_rules`, `parse_cover`, `task_sections`, `build_index`,
`construct_violations`, `enumerate_violations`). Порядок обязателен (§6 «код
`2` старше кода `1`»): сначала разбор и междокументная сверка `member`,
затем индекс снимка, затем пригодность и нарушения **каждого** правила — всё
это код `2`. Только после этого проверяются `dirty` и «протухший» отпечаток
(код `1`, находки-одиночки без вычисления покрытия). Наконец — находки
покрытия: `uncovered`, `duplicate-cover`, `dangling-cover`, `missing-task`,
`site-not-in-task`, `unverifiable`.

Деревья синтетические, как в `test_wiring_enumerates.py`/`test_wiring_resolve.py`:
`Snapshot` собирается прямо из словаря «путь → текст», без git.
"""

from collections.abc import Mapping

import pytest

from disputatio.verifier.wiring import (
    Finding,
    Violation,
    WiringReport,
    check_wiring,
    render_report,
)
from disputatio.verifier.wiring_snapshot import Snapshot, WiringInputError

SRC = "src"
TREE = "70637371794b53ddc85dffb10624838e53ffb07c"

# --- спека и модуль с классом-«нарушителем» -----------------------------------

POLICY_MODULE = f"{SRC}/pkg/policy.py"
RUNNER_MODULE = f"{SRC}/pkg/runner.py"
COMPOSITION_MODULE = f"{SRC}/pkg/composition.py"

POLICY_SOURCE = "class ArchitecturalDefectPolicy:\n    pass\n"
COMPOSITION_SOURCE = (
    "from pkg.policy import ArchitecturalDefectPolicy\n\n\n"
    "def build():\n    return ArchitecturalDefectPolicy()\n"
)
# Два конструктора вне `allowed`: строки 4 и 5 (после импорта и пустой строки).
RUNNER_SOURCE = (
    "from pkg.policy import ArchitecturalDefectPolicy\n\n\n"
    "FIRST = ArchitecturalDefectPolicy()\n"
    "SECOND = ArchitecturalDefectPolicy()\n"
)

CONSTRUCT_SPEC_BODY = f"""\
[[rule]]
id = "p10-policy"
kind = "construct-only-in"
class = "pkg.policy:ArchitecturalDefectPolicy"
allowed = ["{COMPOSITION_MODULE}"]
"""


def _spec(body: str) -> str:
    return f"преамбула\n\n```disputatio-wiring\n{body}```\n\nхвост\n"


def _plan(cover_body: str, *, task_sections_text: str = "") -> str:
    return (
        f"преамбула\n\n```disputatio-wiring-cover\n{cover_body}```\n\n"
        f"{task_sections_text}\nхвост\n"
    )


def _cover_body(src_tree: str, entries: str) -> str:
    return f'src_tree = "{src_tree}"\n\n{entries}'


def _files(**files: str) -> Mapping[str, bytes]:
    return {path: text.encode("utf-8") for path, text in files.items()}


def _snapshot(**files: str) -> Snapshot:
    return Snapshot(tree=TREE, files=_files(**files))


def _check(
    *,
    spec_body: str = CONSTRUCT_SPEC_BODY,
    cover_body: str,
    task_sections_text: str = "",
    files: Mapping[str, bytes] | None = None,
    tree: str = TREE,
    dirty: tuple[str, ...] = (),
) -> WiringReport:
    spec_text = _spec(spec_body)
    plan_text = _plan(cover_body, task_sections_text=task_sections_text)
    snapshot = Snapshot(
        tree=tree,
        files=files
        if files is not None
        else _files(
            **{
                POLICY_MODULE: POLICY_SOURCE,
                COMPOSITION_MODULE: COMPOSITION_SOURCE,
                RUNNER_MODULE: RUNNER_SOURCE,
            }
        ),
    )
    return check_wiring(
        spec_text=spec_text,
        plan_text=plan_text,
        snapshot=snapshot,
        src=SRC,
        dirty=dirty,
    )


# --- 1. `uncovered` ------------------------------------------------------------


def test_uncovered_violation_without_cover_record() -> None:
    """Нарушение без записи покрытия — находка `uncovered` (§5.3.1)."""
    cover_body = _cover_body(TREE, "")

    report = _check(cover_body=cover_body)

    assert report.violations == (
        Violation(
            rule="p10-policy",
            kind="construct-only-in",
            site=f"{RUNNER_MODULE}:4",
            member=None,
            count=1,
        ),
        Violation(
            rule="p10-policy",
            kind="construct-only-in",
            site=f"{RUNNER_MODULE}:5",
            member=None,
            count=1,
        ),
    )
    assert report.findings == (
        Finding(
            code="uncovered",
            rule="p10-policy",
            site=f"{RUNNER_MODULE}:4",
            member=None,
            detail="",
        ),
        Finding(
            code="uncovered",
            rule="p10-policy",
            site=f"{RUNNER_MODULE}:5",
            member=None,
            detail="",
        ),
    )


def test_covered_violation_gives_no_uncovered_finding() -> None:
    """Обе строки покрыты записями — находок `uncovered` нет."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1

[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:5"
task = 1
""",
    )
    task_text = (
        f"### Задача 1: покрыть policy\n"
        f"строка {RUNNER_MODULE}:4 и {RUNNER_MODULE}:5 покрыты композицией\n"
    )

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    assert [f.code for f in report.findings] == []


# --- 2. `duplicate-cover` -------------------------------------------------------


def test_duplicate_cover_records_for_same_key() -> None:
    """Два `[[cover]]` на один ключ — находка `duplicate-cover` (§5.3.2)."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1

[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1
""",
    )
    task_text = f"### Задача 1: t\n{RUNNER_MODULE}:4\n"

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    duplicate = [f for f in report.findings if f.code == "duplicate-cover"]
    assert duplicate == [
        Finding(
            code="duplicate-cover",
            rule="p10-policy",
            site=f"{RUNNER_MODULE}:4",
            member=None,
            detail=duplicate[0].detail,
        )
    ]
    assert duplicate[0].detail != ""


# --- 3. `dangling-cover` ---------------------------------------------------------


def test_dangling_cover_for_site_without_violation() -> None:
    """Запись покрытия ссылается на место, где нарушения нет (§5.3.3)."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1

[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:5"
task = 1

[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:99"
task = 1
""",
    )
    task_text = (
        f"### Задача 1: t\n{RUNNER_MODULE}:4 {RUNNER_MODULE}:5 {RUNNER_MODULE}:99\n"
    )

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    dangling = [f for f in report.findings if f.code == "dangling-cover"]
    assert dangling == [
        Finding(
            code="dangling-cover",
            rule="p10-policy",
            site=f"{RUNNER_MODULE}:99",
            member=None,
            detail=dangling[0].detail,
        )
    ]


def test_dangling_cover_for_undeclared_rule() -> None:
    """Запись покрытия на необъявленный `rule` — тоже `dangling-cover`, не ошибка."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "no-such-rule"
site = "{RUNNER_MODULE}:1"
task = 1
""",
    )
    task_text = f"### Задача 1: t\n{RUNNER_MODULE}:1\n"
    # Нарушения этого правила покрыть нечем: два реальных нарушения p10-policy
    # остаются uncovered, но исключение по необъявленному rule быть не должно.
    report = _check(cover_body=cover_body, task_sections_text=task_text)

    dangling = [f for f in report.findings if f.code == "dangling-cover"]
    assert len(dangling) == 1
    assert dangling[0].rule == "no-such-rule"
    assert dangling[0].site == f"{RUNNER_MODULE}:1"
    assert dangling[0].member is None
    assert dangling[0].detail != ""


# --- 4. `missing-task` ------------------------------------------------------------


def test_missing_task_when_plan_has_no_such_heading() -> None:
    """`task` записи покрытия не найден среди заголовков плана (§5.3.4)."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 7
""",
    )

    report = _check(cover_body=cover_body)

    missing = [f for f in report.findings if f.code == "missing-task"]
    assert missing == [
        Finding(
            code="missing-task",
            rule="p10-policy",
            site=f"{RUNNER_MODULE}:4",
            member=None,
            detail=missing[0].detail,
        )
    ]
    assert "7" in missing[0].detail


def test_missing_task_when_heading_is_only_inside_fenced_example() -> None:
    """4-пробельный `\\`\\`\\`` внутри примера фенс раньше времени не закрывает.

    По CommonMark строка фенса — не более 3 пробелов отступа. Если бы отступ
    не проверялся, `"    ```"` закрыла бы блок примера раньше настоящей
    закрывающей строки, и текст `### Task 1: пример` вместе с местом внутри
    примера стал бы прозой — мнимым заголовком задачи 1, хотя настоящей
    задачи 1 в плане нет. Ожидание: заголовок остаётся скрыт фенсом целиком,
    и запись покрытия на несуществующую задачу даёт `missing-task`.
    """
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1
""",
    )
    task_text = f"```text\n    ```\n### Task 1: пример\n{RUNNER_MODULE}:4\n```\n"

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    missing = [f for f in report.findings if f.code == "missing-task"]
    assert missing != []
    assert report.findings != ()


# --- 5. `site-not-in-task` ---------------------------------------------------------


def test_site_not_in_task_when_referenced_elsewhere() -> None:
    """Задача есть, но `site` встречается в другом разделе, не в своём (§5.3.5)."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1
""",
    )
    task_text = (
        "### Задача 1: пустая\nничего по теме\n\n"
        f"### Задача 2: другая\n{RUNNER_MODULE}:4 упомянуто тут\n"
    )

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    site_not_in_task = [f for f in report.findings if f.code == "site-not-in-task"]
    assert site_not_in_task == [
        Finding(
            code="site-not-in-task",
            rule="p10-policy",
            site=f"{RUNNER_MODULE}:4",
            member=None,
            detail=site_not_in_task[0].detail,
        )
    ]


ENUM_MODULE = f"{SRC}/pkg/guard.py"
ENUM_SOURCE = (
    "def _guard_history(previous, current) -> None:\n"
    '    _check(previous.a, current.a, "a")\n'
)

ENUM_SPEC_BODY = f"""\
[[rule]]
id = "append-only-guard"
kind = "enumerates-all"
module = "{ENUM_MODULE}"
function = "_guard_history"
checkers = ["_check"]
members = ["a", "b"]
"""


def test_site_not_in_task_missing_member_for_enumerates_all() -> None:
    """`enumerates-all`: `site` в разделе есть, но имени `member` нет (§5.3.5)."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "append-only-guard"
site = "{ENUM_MODULE}:1"
member = "b"
task = 1
""",
    )
    task_text = f"### Задача 1: t\n{ENUM_MODULE}:1 упомянуто, но не имя члена\n"

    report = _check(
        spec_body=ENUM_SPEC_BODY,
        cover_body=cover_body,
        task_sections_text=task_text,
        files=_files(**{ENUM_MODULE: ENUM_SOURCE}),
    )

    site_not_in_task = [f for f in report.findings if f.code == "site-not-in-task"]
    assert len(site_not_in_task) == 1
    assert site_not_in_task[0].member == "b"


# --- сочетания находок: duplicate/dangling × missing-task/site-not-in-task -----
#
# `missing-task`/`site-not-in-task` считаются по каждой записи `cover`
# независимо от того, дублирует она другую запись или висит без нарушения
# (`_coverage_findings` не пропускает такие записи) — а `duplicate-cover` и
# `dangling-cover` не гасят друг друга на одном ключе. Ни то, ни другое не
# было зафиксировано тестами выше: там разделы задач уже содержали `site`.


def test_dangling_cover_with_missing_task_gives_both_findings() -> None:
    """Висячая запись на несуществующий номер задачи — обе находки сразу
    (§5.3.3-4): `dangling-cover` и `missing-task` не гасят друг друга."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:99"
task = 7
""",
    )

    report = _check(cover_body=cover_body)

    key = ("p10-policy", f"{RUNNER_MODULE}:99", None)
    dangling = [
        f
        for f in report.findings
        if f.code == "dangling-cover" and (f.rule, f.site, f.member) == key
    ]
    missing_task = [
        f
        for f in report.findings
        if f.code == "missing-task" and (f.rule, f.site, f.member) == key
    ]
    assert len(dangling) == 1
    assert len(missing_task) == 1
    assert "7" in missing_task[0].detail


def test_dangling_cover_with_site_not_in_task_gives_both_findings() -> None:
    """Висячая запись, чей раздел задачи не ссылается на `site`, — обе
    находки сразу (§5.3.3, §5.3.5)."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:99"
task = 1
""",
    )
    task_text = "### Задача 1: t\nничего по теме\n"

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    key = ("p10-policy", f"{RUNNER_MODULE}:99", None)
    dangling = [
        f
        for f in report.findings
        if f.code == "dangling-cover" and (f.rule, f.site, f.member) == key
    ]
    site_not_in_task = [
        f
        for f in report.findings
        if f.code == "site-not-in-task" and (f.rule, f.site, f.member) == key
    ]
    assert len(dangling) == 1
    assert len(site_not_in_task) == 1


def test_duplicate_cover_where_one_record_points_to_missing_task() -> None:
    """Дубль ключа, у которого одна из записей ссылается на несуществующую
    задачу, — `duplicate-cover` и `missing-task` вместе, каждый по своей
    причине (§5.2, §5.3.4); нарушение остаётся покрытым, не `uncovered`."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1

[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 99
""",
    )
    task_text = f"### Задача 1: t\n{RUNNER_MODULE}:4\n"

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    key = ("p10-policy", f"{RUNNER_MODULE}:4", None)
    duplicate = [
        f
        for f in report.findings
        if f.code == "duplicate-cover" and (f.rule, f.site, f.member) == key
    ]
    missing_task = [
        f
        for f in report.findings
        if f.code == "missing-task" and (f.rule, f.site, f.member) == key
    ]
    uncovered_for_key = [
        f
        for f in report.findings
        if f.code == "uncovered" and (f.rule, f.site, f.member) == key
    ]
    assert len(duplicate) == 1
    assert len(missing_task) == 1
    assert "99" in missing_task[0].detail
    assert uncovered_for_key == []


def test_key_both_duplicated_and_dangling_gives_both_findings() -> None:
    """Ключ с двумя записями и без единого нарушения на нём — `duplicate-cover`
    и `dangling-cover` вместе, ни одна находка не подавляет другую (§5.2,
    §5.3.2-3)."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:99"
task = 1

[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:99"
task = 1
""",
    )
    task_text = f"### Задача 1: t\n{RUNNER_MODULE}:99\n"

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    key = ("p10-policy", f"{RUNNER_MODULE}:99", None)
    codes = {f.code for f in report.findings if (f.rule, f.site, f.member) == key}
    assert codes == {"duplicate-cover", "dangling-cover"}


# --- `dirty`/`stale` коротят вычисление покрытия --------------------------------


def test_dirty_gives_only_dirty_src_finding_no_coverage() -> None:
    """`dirty` непуст → только находка `dirty-src`, покрытие не вычисляется."""
    cover_body = _cover_body(TREE, "")

    report = _check(cover_body=cover_body, dirty=(RUNNER_MODULE,))

    assert report.violations == ()
    assert [f.code for f in report.findings] == ["dirty-src"]


def test_stale_snapshot_gives_only_stale_finding_no_coverage() -> None:
    """Отпечаток плана не совпал со снимком → только `stale-snapshot`."""
    cover_body = _cover_body("0" * 40, "")

    report = _check(cover_body=cover_body)

    assert report.violations == ()
    assert [f.code for f in report.findings] == ["stale-snapshot"]


# --- код `2` старше кода `1` -----------------------------------------------------


def test_dirty_does_not_hide_unparseable_python_file() -> None:
    """Грязный `src` + неразбираемый `.py` → `WiringInputError`, не `dirty-src`."""
    cover_body = _cover_body(TREE, "")
    broken_files = _files(
        **{
            POLICY_MODULE: POLICY_SOURCE,
            COMPOSITION_MODULE: COMPOSITION_SOURCE,
            RUNNER_MODULE: "def broken(:\n",
        }
    )

    with pytest.raises(WiringInputError):
        _check(cover_body=cover_body, files=broken_files, dirty=(RUNNER_MODULE,))


def test_dirty_does_not_hide_missing_rule_class() -> None:
    """Грязный `src` + класс правила отсутствует в снимке → `WiringInputError`."""
    cover_body = _cover_body(TREE, "")
    files_without_policy = _files(
        **{COMPOSITION_MODULE: COMPOSITION_SOURCE, RUNNER_MODULE: RUNNER_SOURCE}
    )

    with pytest.raises(WiringInputError):
        _check(
            cover_body=cover_body, files=files_without_policy, dirty=(RUNNER_MODULE,)
        )


def test_stale_does_not_hide_missing_allowed_path() -> None:
    """Протухший отпечаток + отсутствующий путь `allowed` → `WiringInputError`."""
    spec_body = f"""\
[[rule]]
id = "p10-policy"
kind = "construct-only-in"
class = "pkg.policy:ArchitecturalDefectPolicy"
allowed = ["{SRC}/pkg/does_not_exist.py"]
"""
    cover_body = _cover_body("0" * 40, "")

    with pytest.raises(WiringInputError):
        _check(spec_body=spec_body, cover_body=cover_body)


def test_stale_does_not_hide_duplicate_task_number() -> None:
    """Протухший отпечаток + повтор номера задачи в плане → `WiringInputError`."""
    cover_body = _cover_body("0" * 40, "")
    task_text = "### Задача 1: раз\nтекст\n\n### Задача 1: два\nтекст\n"

    with pytest.raises(WiringInputError):
        _check(cover_body=cover_body, task_sections_text=task_text)


# --- междокументная сверка `member` ----------------------------------------------


def test_member_on_construct_only_in_cover_is_input_error() -> None:
    """`member` у записи покрытия `construct-only-in` — `WiringInputError` (§5.1)."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
member = "oops"
task = 1
""",
    )

    with pytest.raises(WiringInputError):
        _check(cover_body=cover_body)


def test_missing_member_on_enumerates_all_cover_is_input_error() -> None:
    """Отсутствие `member` у записи покрытия `enumerates-all` — `WiringInputError`."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "append-only-guard"
site = "{ENUM_MODULE}:1"
task = 1
""",
    )

    with pytest.raises(WiringInputError):
        _check(
            spec_body=ENUM_SPEC_BODY,
            cover_body=cover_body,
            files=_files(**{ENUM_MODULE: ENUM_SOURCE}),
        )


# --- `Unverifiable` ---------------------------------------------------------------

UNVERIFIABLE_SOURCE = (
    "def _guard_history(previous, current) -> None:\n"
    "    if previous:\n"
    '        _check(previous.a, current.a, "a")\n'
)


def test_unverifiable_gives_finding_and_cover_on_it_is_dangling() -> None:
    """`Unverifiable` → находка `unverifiable`; покрытие на это место —
    `dangling-cover`."""
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "append-only-guard"
site = "{ENUM_MODULE}:1"
member = "a"
task = 1
""",
    )
    task_text = f"### Задача 1: t\n{ENUM_MODULE}:1 a\n"

    report = _check(
        spec_body=ENUM_SPEC_BODY,
        cover_body=cover_body,
        task_sections_text=task_text,
        files=_files(**{ENUM_MODULE: UNVERIFIABLE_SOURCE}),
    )

    codes = sorted(f.code for f in report.findings)
    assert codes == ["dangling-cover", "unverifiable"]
    assert report.violations == ()


# --- `render_report` ---------------------------------------------------------------


def test_render_report_uncovered_construct_only_in_line_format() -> None:
    """Формат строки `uncovered` для `construct-only-in` — дословно из design §6."""
    report = WiringReport(
        violations=(
            Violation(
                rule="p10-policy",
                kind="construct-only-in",
                site="src/disputatio/runtime/pipeline_runner.py:1071",
                member=None,
                count=1,
            ),
        ),
        findings=(
            Finding(
                code="uncovered",
                rule="p10-policy",
                site="src/disputatio/runtime/pipeline_runner.py:1071",
                member=None,
                detail="",
            ),
        ),
    )

    lines = render_report(report)

    assert lines[0] == (
        "uncovered construct-only-in p10-policy "
        "src/disputatio/runtime/pipeline_runner.py:1071 count=1"
    )
    assert lines[-1] == "wiring: 1 нарушений, 0 покрыто, 1 находок"


def test_render_report_sorts_by_code_rule_site_member() -> None:
    """Строки находок отсортированы по `(код, rule, site, member)` (§6)."""
    findings = (
        Finding(code="uncovered", rule="b-rule", site="s:2", member=None, detail=""),
        Finding(code="uncovered", rule="a-rule", site="s:1", member=None, detail=""),
        Finding(
            code="dangling-cover", rule="a-rule", site="s:1", member=None, detail="x"
        ),
    )
    report = WiringReport(violations=(), findings=findings)

    lines = render_report(report)

    codes_in_order = [line.split()[0] for line in lines[:-1]]
    assert codes_in_order == ["dangling-cover", "uncovered", "uncovered"]
    assert "a-rule" in lines[1]
    assert "b-rule" in lines[2]


def test_render_report_summary_line_is_last() -> None:
    """Последняя строка — сводка `wiring: N нарушений, M покрыто, K находок`."""
    report = WiringReport(violations=(), findings=())

    lines = render_report(report)

    assert lines == ["wiring: 0 нарушений, 0 покрыто, 0 находок"]


# --- ссылка на место: fenced-текст не засчитывается (§5.3) -----------------------


def _site_not_in_task_with_plan(plan_text: str) -> list[Finding]:
    """Находки `site-not-in-task` для покрытия строки 4 раннера задачей 1."""
    report = check_wiring(
        spec_text=_spec(CONSTRUCT_SPEC_BODY),
        plan_text=plan_text,
        snapshot=_snapshot(
            **{
                POLICY_MODULE: POLICY_SOURCE,
                COMPOSITION_MODULE: COMPOSITION_SOURCE,
                RUNNER_MODULE: RUNNER_SOURCE.replace("SECOND = ", "# "),
            }
        ),
        src=SRC,
        dirty=(),
    )
    assert [f.code for f in report.findings if f.code == "uncovered"] == []
    return [f for f in report.findings if f.code == "site-not-in-task"]


_COVER_LINE_4 = f"""\
```disputatio-wiring-cover
src_tree = "{TREE}"

[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1
```
"""


def test_cover_block_inside_task_section_is_not_a_link() -> None:
    """Блок покрытия внутри раздела задачи сам по себе ссылкой не считается."""
    plan_text = f"# План\n\n### Task 1: unrelated work\n\n{_COVER_LINE_4}\n"

    findings = _site_not_in_task_with_plan(plan_text)

    assert [f.site for f in findings] == [f"{RUNNER_MODULE}:4"]


def test_site_only_in_fenced_example_is_not_a_link() -> None:
    """Место упомянуто только в fenced-примере раздела — ссылки нет."""
    plan_text = (
        f"# План\n\n{_COVER_LINE_4}\n"
        "### Задача 1: пример\n"
        f"```text\n{RUNNER_MODULE}:4\n```\n"
    )

    findings = _site_not_in_task_with_plan(plan_text)

    assert [f.site for f in findings] == [f"{RUNNER_MODULE}:4"]


def test_site_in_prose_next_to_cover_block_is_a_link() -> None:
    """Место в прозе раздела — ссылка есть, даже если блок покрытия рядом."""
    plan_text = (
        "# План\n\n### Задача 1: убрать конструктор\n"
        f"Правка `{RUNNER_MODULE}:4`.\n\n{_COVER_LINE_4}\n"
    )

    assert _site_not_in_task_with_plan(plan_text) == []


# --- ссылка на место и член — целым токеном, а не подстрокой (§5.3) --------------


def _runner_with_constructor_on_line(line: int) -> str:
    """Раннер, где единственный конструктор стоит ровно на строке `line`."""
    padding = "\n" * (line - 2)
    return (
        f"from pkg.policy import ArchitecturalDefectPolicy\n{padding}"
        "BAD = ArchitecturalDefectPolicy()\n"
    )


def _site_findings_for_mention(line: int, mention: str) -> list[Finding]:
    """`site-not-in-task` покрытия строки `line`, когда задача пишет `mention`."""
    site = f"{RUNNER_MODULE}:{line}"
    cover_body = _cover_body(
        TREE, f'[[cover]]\nrule = "p10-policy"\nsite = "{site}"\ntask = 1\n'
    )
    report = _check(
        cover_body=cover_body,
        task_sections_text=f"### Задача 1: t\nправка {mention} здесь\n",
        files=_files(
            **{
                POLICY_MODULE: POLICY_SOURCE,
                COMPOSITION_MODULE: COMPOSITION_SOURCE,
                RUNNER_MODULE: _runner_with_constructor_on_line(line),
            }
        ),
    )
    assert [f.code for f in report.findings if f.code == "uncovered"] == []
    return [f for f in report.findings if f.code == "site-not-in-task"]


@pytest.mark.parametrize(
    ("line", "mention"),
    [
        (8, f"{RUNNER_MODULE}:80"),
        (58, f"{RUNNER_MODULE}:581"),
        (8, f"x{RUNNER_MODULE}:8"),
        (8, f"other/{RUNNER_MODULE}:8"),
        (8, f"old-{RUNNER_MODULE}:8"),
        (8, f"{SRC}/pkg/runnerXpy:8"),
    ],
)
def test_site_prefix_of_longer_token_is_not_a_link(line: int, mention: str) -> None:
    """`b.py:8` внутри `b.py:80` или `x/b.py:8` — не ссылка на место (§5.3)."""
    assert len(_site_findings_for_mention(line, mention)) == 1


@pytest.mark.parametrize(
    "template",
    [
        "`{site}`",
        "({site})",
        "{site}.",
        "{site},",
        "{site}:12",
        "[{site}]",
    ],
)
def test_site_with_punctuation_around_is_a_link(template: str) -> None:
    """Место в кавычках, скобках, перед точкой/запятой — ссылка есть (§5.3)."""
    mention = template.format(site=f"{RUNNER_MODULE}:8")

    assert _site_findings_for_mention(8, mention) == []


MEMBER_SPEC_BODY = f"""\
[[rule]]
id = "append-only-guard"
kind = "enumerates-all"
module = "{ENUM_MODULE}"
function = "_guard_history"
checkers = ["_check"]
members = ["doc_sessions", "sessions"]
"""
MEMBER_SOURCE = (
    "def _guard_history(previous, current) -> None:\n"
    '    _check(previous.doc_sessions, current.doc_sessions, "d")\n'
)


def _member_findings_for_mention(mention: str) -> list[Finding]:
    """`site-not-in-task` покрытия члена `sessions`, когда задача пишет `mention`."""
    cover_body = _cover_body(
        TREE,
        f'[[cover]]\nrule = "append-only-guard"\nsite = "{ENUM_MODULE}:1"\n'
        'member = "sessions"\ntask = 1\n',
    )
    report = _check(
        spec_body=MEMBER_SPEC_BODY,
        cover_body=cover_body,
        task_sections_text=f"### Задача 1: t\n{ENUM_MODULE}:1 — {mention}\n",
        files=_files(**{ENUM_MODULE: MEMBER_SOURCE}),
    )
    assert [f.code for f in report.findings if f.code == "uncovered"] == []
    return [f for f in report.findings if f.code == "site-not-in-task"]


@pytest.mark.parametrize("mention", ["doc_sessions", "sessions2", "sessionsы"])
def test_member_inside_longer_word_is_not_a_link(mention: str) -> None:
    """`sessions` внутри `doc_sessions` — не ссылка на член (§5.3)."""
    findings = _member_findings_for_mention(mention)

    assert [f.member for f in findings] == ["sessions"]


@pytest.mark.parametrize(
    "mention", ["`sessions`", "(sessions)", "sessions.", "sessions,", "x.sessions"]
)
def test_member_with_punctuation_around_is_a_link(mention: str) -> None:
    """Член в кавычках, скобках, как атрибут — ссылка есть (§5.3)."""
    assert _member_findings_for_mention(mention) == []


def test_missing_task_when_heading_is_only_inside_html_comment() -> None:
    """Невидимый заголовок в HTML-комментарии задачу не создаёт (§5.3).

    Воспроизведение находки ревью: `<!--\\n### Task 1: …\\nместо\\n-->` давал
    задачу 1 со ссылкой на место — гейт проходил без видимой задачи.
    """
    cover_body = _cover_body(
        TREE,
        f"""\
[[cover]]
rule = "p10-policy"
site = "{RUNNER_MODULE}:4"
task = 1
""",
    )
    task_text = f"<!--\n### Task 1: скрытая\n{RUNNER_MODULE}:4\n-->\n"

    report = _check(cover_body=cover_body, task_sections_text=task_text)

    missing = [f for f in report.findings if f.code == "missing-task"]
    assert missing != []
