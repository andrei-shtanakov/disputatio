"""Приёмка гейта wiring на историческом снимке 23c9297 (design §7).

Обязательные приёмочные примеры собраны из **полного** дерева `src` снимка
`23c9297` (база PR #54, тот самый снимок, на котором SPEC-002 v0.2 и план
пропустили оба дефекта — design §1). Дерево вендорится в тесты архивом
`tests/fixtures/wiring/src-23c9297.tar.gz` (см. `tests/fixtures/wiring/README.md`
про происхождение и команду пересборки).

Тест дословности (`TestFixtureFingerprint`) обязан идти первым в модуле и
проходить раньше всех прочих: он сверяет отпечаток развёрнутой фикстуры с
`70637371794b53ddc85dffb10624838e53ffb07c` (design §7). Совпадение доказывает
две вещи разом — что архив несёт именно то дерево, и что номера строк ниже
(`581`, `1071`, `109`) исторические, а не выдуманные. Провал этого теста
обесценивает все остальные: правила ниже целятся именно в эти координаты.

Правила — дословно пример design §3.1: `p10-policy` (`construct-only-in` над
`ArchitecturalDefectPolicy`) и `append-only-guard` (`enumerates-all` над
`_guard_history`). На полном дереве они дают ровно три нарушения (§7,
сверено по всем 81 модулю снимка) — обе строки конструктора (581, 1071) и
`doc_sessions` на строке 109. Таблица §7 проверяется впятером: по одному
тесту на строку, синтетические планы покрывают то подмножество нарушений,
которое называет строка таблицы.
"""

import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from disputatio.verifier.wiring import (
    Finding,
    Violation,
    WiringReport,
    check_wiring,
    render_report,
)
from disputatio.verifier.wiring_snapshot import read_snapshot, src_fingerprint

SRC = "src"

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "wiring"
FIXTURE_TAR = _FIXTURES_DIR / "src-23c9297.tar.gz"

#: Отпечаток `HEAD:src` коммита `23c929776192e82e7463dd204743dd484694bc8d`
#: (design §5.1, §7) — тот же, что несёт `src_tree` в блоке покрытия примера.
EXPECTED_FINGERPRINT = "70637371794b53ddc85dffb10624838e53ffb07c"

SITE_CONSTRUCT_FIRST = "src/disputatio/runtime/pipeline_runner.py:581"
SITE_CONSTRUCT_SECOND = "src/disputatio/runtime/pipeline_runner.py:1071"
SITE_ENUMERATE = "src/disputatio/events/pipeline_store.py:109"
MEMBER_ENUMERATE = "doc_sessions"

# Правила — дословно пример design §3.1.
RULES_BODY = """\
[[rule]]
id = "p10-policy"
kind = "construct-only-in"
class = "disputatio.runtime.pipeline_runner:ArchitecturalDefectPolicy"
allowed = ["src/disputatio/runtime/composition.py"]

[[rule]]
id = "append-only-guard"
kind = "enumerates-all"
module = "src/disputatio/events/pipeline_store.py"
function = "_guard_history"
checkers = ["_guard_sessions", "_guard_immutable"]
members = ["spec_sessions", "pair_sessions", "doc_sessions",
           "transitions", "operator_decisions"]
"""

SPEC_TEXT = f"# Спека\n\n```disputatio-wiring\n{RULES_BODY}```\n"


@pytest.fixture
def historical_repo(tmp_path: Path, git_run: Callable[..., str]) -> Path:
    """Дерево `src` снимка `23c9297`: архив → временный git-репозиторий (§7).

    Разворачивает `src-23c9297.tar.gz` стандартным `tarfile` прямо в
    `tmp_path` (архив уже несёт префикс `src/` — см. README фикстуры),
    затем `git init` и один коммит: координаты нарушений (§3.2, §3.5)
    привязаны к дереву `HEAD:<src>` (§4), а не к файловой системе.
    Identity коммита задаётся флагами `-c`, а не глобальным конфигом:
    репозиторий тестов герметичен (`git_run`/`conftest._HERMETIC_GIT_ENV`).
    """
    with tarfile.open(FIXTURE_TAR, "r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    git_run(tmp_path, "init", "--quiet")
    git_run(tmp_path, "add", "-A")
    git_run(
        tmp_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "--quiet",
        "-m",
        "snapshot 23c9297",
    )
    return tmp_path


class TestFixtureFingerprint:
    """Тест дословности — обязан идти первым в модуле (design §7, брифинг)."""

    def test_fingerprint_matches_historical_tree(self, historical_repo: Path) -> None:
        assert src_fingerprint(historical_repo, SRC) == EXPECTED_FINGERPRINT


def _plan(src_tree: str, *, cover_body: str = "", task_sections: str = "") -> str:
    """План: блок `disputatio-wiring-cover` (§5.1) + произвольные разделы задач."""
    return (
        "# План\n\n```disputatio-wiring-cover\n"
        f'src_tree = "{src_tree}"\n\n'
        f"{cover_body}```\n\n{task_sections}"
    )


def _report(historical_repo: Path, plan_text: str) -> WiringReport:
    snapshot = read_snapshot(historical_repo, SRC)
    assert snapshot.tree == EXPECTED_FINGERPRINT
    return check_wiring(
        spec_text=SPEC_TEXT,
        plan_text=plan_text,
        snapshot=snapshot,
        src=SRC,
        dirty=(),
    )


class TestFullTreeViolations:
    """«На полном дереве снимка правила дают ровно три нарушения» (§7)."""

    def test_exactly_three_violations_no_extra(self, historical_repo: Path) -> None:
        # Покрытие роли не играет для подсчёта нарушений — они считаются до
        # шагов dirty/stale-snapshot (`check_wiring` design §6, шаг 3 раньше
        # шагов 4-5). Пустой блок покрытия достаточен.
        report = _report(historical_repo, _plan(EXPECTED_FINGERPRINT))

        assert set(report.violations) == {
            Violation(
                rule="p10-policy",
                kind="construct-only-in",
                site=SITE_CONSTRUCT_FIRST,
                member=None,
                count=1,
            ),
            Violation(
                rule="p10-policy",
                kind="construct-only-in",
                site=SITE_CONSTRUCT_SECOND,
                member=None,
                count=1,
            ),
            Violation(
                rule="append-only-guard",
                kind="enumerates-all",
                site=SITE_ENUMERATE,
                member=MEMBER_ENUMERATE,
                count=1,
            ),
        }


class TestAcceptanceTable:
    """Пять строк таблицы §7 — по тесту на строку."""

    def test_second_constructor_partial_cover_gives_uncovered(
        self, historical_repo: Path
    ) -> None:
        """«второй конструктор», план покрывает только строку 581 (§7 строка 1)."""
        cover_body = f"""\
[[cover]]
rule = "p10-policy"
site = "{SITE_CONSTRUCT_FIRST}"
task = 1

"""
        task_sections = (
            "### Задача 1: снять первый конструктор\n\n"
            f"Место `{SITE_CONSTRUCT_FIRST}`.\n"
        )
        plan_text = _plan(
            EXPECTED_FINGERPRINT, cover_body=cover_body, task_sections=task_sections
        )

        report = _report(historical_repo, plan_text)

        assert report.findings  # код 1: непустой набор находок
        assert (
            Finding(
                code="uncovered",
                rule="p10-policy",
                site=SITE_CONSTRUCT_SECOND,
                member=None,
                detail="",
            )
            in report.findings
        )
        assert not any(
            f.code == "uncovered"
            and f.rule == "p10-policy"
            and f.site == SITE_CONSTRUCT_FIRST
            for f in report.findings
        )

    def test_second_constructor_full_cover_leaves_rule_clean(
        self, historical_repo: Path
    ) -> None:
        """«второй конструктор», план покрывает 581 и 1071 (§7 строка 2)."""
        cover_body = f"""\
[[cover]]
rule = "p10-policy"
site = "{SITE_CONSTRUCT_FIRST}"
task = 1

[[cover]]
rule = "p10-policy"
site = "{SITE_CONSTRUCT_SECOND}"
task = 1

"""
        task_sections = (
            "### Задача 1: снять оба конструктора\n\n"
            f"Места `{SITE_CONSTRUCT_FIRST}` и `{SITE_CONSTRUCT_SECOND}`.\n"
        )
        plan_text = _plan(
            EXPECTED_FINGERPRINT, cover_body=cover_body, task_sections=task_sections
        )

        report = _report(historical_repo, plan_text)

        # Позитивная проверка: правило действительно нашло оба нарушения —
        # без неё тест проходил бы и на гейте, который вообще ничего не
        # находит (пустой отчёт тоже не содержит `rule == "p10-policy"`).
        assert (
            Violation(
                rule="p10-policy",
                kind="construct-only-in",
                site=SITE_CONSTRUCT_FIRST,
                member=None,
                count=1,
            )
            in report.violations
        )
        assert (
            Violation(
                rule="p10-policy",
                kind="construct-only-in",
                site=SITE_CONSTRUCT_SECOND,
                member=None,
                count=1,
            )
            in report.violations
        )
        assert not any(f.rule == "p10-policy" for f in report.findings)

    def test_incomplete_enumeration_no_cover_gives_uncovered(
        self, historical_repo: Path
    ) -> None:
        """«неполное перечисление», план ничего не покрывает (§7 строка 3)."""
        plan_text = _plan(EXPECTED_FINGERPRINT)

        report = _report(historical_repo, plan_text)

        assert report.findings  # код 1: непустой набор находок
        assert (
            Finding(
                code="uncovered",
                rule="append-only-guard",
                site=SITE_ENUMERATE,
                member=MEMBER_ENUMERATE,
                detail="",
            )
            in report.findings
        )

    def test_incomplete_enumeration_full_cover_leaves_rule_clean(
        self, historical_repo: Path
    ) -> None:
        """«неполное перечисление», план покрывает строку 109 (§7 строка 4)."""
        cover_body = f"""\
[[cover]]
rule = "append-only-guard"
site = "{SITE_ENUMERATE}"
member = "{MEMBER_ENUMERATE}"
task = 1

"""
        task_sections = (
            "### Задача 1: добавить пятую коллекцию в перебор\n\n"
            f"Место `{SITE_ENUMERATE}`, член `{MEMBER_ENUMERATE}`.\n"
        )
        plan_text = _plan(
            EXPECTED_FINGERPRINT, cover_body=cover_body, task_sections=task_sections
        )

        report = _report(historical_repo, plan_text)

        # Позитивная проверка: правило действительно нашло нарушение —
        # без неё тест проходил бы и на гейте, который вообще ничего не
        # находит (пустой отчёт тоже не содержит `rule == "append-only-guard"`).
        assert (
            Violation(
                rule="append-only-guard",
                kind="enumerates-all",
                site=SITE_ENUMERATE,
                member=MEMBER_ENUMERATE,
                count=1,
            )
            in report.violations
        )
        assert not any(f.rule == "append-only-guard" for f in report.findings)

    def test_both_rules_fully_covered_gives_no_findings(
        self, historical_repo: Path
    ) -> None:
        """«оба правила», план покрывает все три нарушения (§7 строка 5) → код 0."""
        cover_body = f"""\
[[cover]]
rule = "p10-policy"
site = "{SITE_CONSTRUCT_FIRST}"
task = 5

[[cover]]
rule = "p10-policy"
site = "{SITE_CONSTRUCT_SECOND}"
task = 5

[[cover]]
rule = "append-only-guard"
site = "{SITE_ENUMERATE}"
member = "{MEMBER_ENUMERATE}"
task = 1

"""
        task_sections = (
            "### Задача 5: снять оба конструктора вне composition root\n\n"
            f"Места `{SITE_CONSTRUCT_FIRST}` и `{SITE_CONSTRUCT_SECOND}`.\n\n"
            "### Задача 1: добавить пятую коллекцию в перебор\n\n"
            f"Место `{SITE_ENUMERATE}`, член `{MEMBER_ENUMERATE}`.\n"
        )
        plan_text = _plan(
            EXPECTED_FINGERPRINT, cover_body=cover_body, task_sections=task_sections
        )

        report = _report(historical_repo, plan_text)

        assert report.findings == ()
        assert len(report.violations) == 3
        # Вывод гейта тоже кончается сводкой без находок (§6) — печатается,
        # чтобы формат `render_report` был проверен и на «зелёном» отчёте.
        assert render_report(report)[-1] == "wiring: 3 нарушений, 3 покрыто, 0 находок"
