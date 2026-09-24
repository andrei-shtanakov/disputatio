"""`disp gate wiring` — подкоманда CLI гейта покрытия (design §2, §2.1, §6).

Обработчик — тонкая проводка поверх уже готового `verifier.wiring`: читает
`--spec`/`--plan` как UTF-8, строит снимок и статус грязи `--src` через
`verifier.wiring_snapshot`, зовёт `check_wiring`, печатает `render_report`.
Всё содержательное поведение самих правил и покрытия уже покрыто
`tests/verifier/test_wiring_coverage.py` и соседями — здесь проверяется
именно ПРОВОДКА: коды возврата (§6), формат отказа `WiringInputError` (одна
строка, без traceback, код `2` старше кода `1`), read-only гейта и то, что
`run_gate` реального `disp` действительно находит и гоняет эту подкоманду
(§2.1).

`WiringInputError` не наследует `DisputatioError` (`verifier` не
импортирует `runtime`, INV-10), поэтому она обязана ловиться внутри
`cmd_gate_wiring`, а не в `main`: тест `test_missing_spec_file_exits_error`
и мутация в отчёте задачи проверяют именно это — что подмена перехвата на
`return EXIT_OK` красит тест.
"""

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from disputatio.cli import EXIT_ERROR, EXIT_FAILED, EXIT_OK, main
from disputatio.contracts.verification import GateStatus
from disputatio.verifier.config import GateSpec
from disputatio.verifier.runner import run_gate

SRC = "src"

POLICY_SOURCE = "class Policy:\n    pass\n"
COMPOSITION_SOURCE = (
    "from pkg.policy import Policy\n\n\ndef build():\n    return Policy()\n"
)
# Конструктор вне `allowed` — строка 3 (после импорта и пустой строки).
RUNNER_SOURCE = "from pkg.policy import Policy\n\nBAD = Policy()\n"

RULE_BODY = """\
[[rule]]
id = "p1-policy"
kind = "construct-only-in"
class = "pkg.policy:Policy"
allowed = ["src/pkg/composition.py"]
"""


def _write(repo: Path, relpath: str, content: str) -> None:
    """Пишет `content` в `repo/relpath`, создавая недостающие каталоги."""
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    """`git *args` в `repo`; ненулевой код возврата — ошибка фикстуры."""
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return completed.stdout


def _commit(repo: Path, message: str) -> None:
    """Коммитит всё дерево `repo` — идентичность уже задана `git_repo`."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)


def _tree(repo: Path, src: str = SRC) -> str:
    """Отпечаток `HEAD:<src>` — тот же, что кладёт `--spec`/план в `src_tree`."""
    return _git(repo, "rev-parse", f"HEAD:{src}").strip()


def _spec(body: str = RULE_BODY) -> str:
    return f"# Спека\n\n```disputatio-wiring\n{body}```\n"


def _plan(src_tree: str, cover_body: str = "") -> str:
    return (
        "# План\n\n```disputatio-wiring-cover\n"
        f'src_tree = "{src_tree}"\n\n{cover_body}```\n'
    )


@pytest.fixture
def wiring_repo(git_repo: Path) -> Path:
    """`git_repo` + модуль `pkg.policy`/`pkg.composition` без нарушений."""
    _write(git_repo, "src/pkg/__init__.py", "")
    _write(git_repo, "src/pkg/policy.py", POLICY_SOURCE)
    _write(git_repo, "src/pkg/composition.py", COMPOSITION_SOURCE)
    _commit(git_repo, "add src")
    return git_repo


def _run_gate_cli(root: Path, *, spec: str = "spec.md", plan: str = "plan.md") -> int:
    return main(
        [
            "gate",
            "wiring",
            "--spec",
            spec,
            "--plan",
            plan,
            "--root",
            str(root),
        ]
    )


# --- §6: коды 0/1 --------------------------------------------------------------


class TestExitCodes:
    def test_covered_tree_exits_ok_with_summary_tail(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Дерево без нарушений: код `0`, stdout кончается сводкой (§6)."""
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        _write(wiring_repo, "plan.md", _plan(tree))

        code = _run_gate_cli(wiring_repo)

        assert code == EXIT_OK
        lines = capsys.readouterr().out.strip("\n").splitlines()
        assert lines[-1] == "wiring: 0 нарушений, 0 покрыто, 0 находок"

    def test_uncovered_violation_exits_failed(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Непокрытое нарушение: код `1`, строка `uncovered …` (§6)."""
        _write(wiring_repo, "src/pkg/runner.py", RUNNER_SOURCE)
        _commit(wiring_repo, "add violation")
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        _write(wiring_repo, "plan.md", _plan(tree))

        code = _run_gate_cli(wiring_repo)

        assert code == EXIT_FAILED
        lines = capsys.readouterr().out.strip("\n").splitlines()
        assert "uncovered construct-only-in p1-policy src/pkg/runner.py:3 count=1" in (
            lines
        )
        assert lines[-1] == "wiring: 1 нарушений, 0 покрыто, 1 находок"


# --- §6: код 2 — по одной причине из таблицы -----------------------------------


def _assert_exits_error_one_line(
    capsys: pytest.CaptureFixture[str], root: Path, *, spec: str, plan: str
) -> str:
    """Гоняет гейт, требует код `2` и ровно одну строку в stdout и в stderr."""
    code = _run_gate_cli(root, spec=spec, plan=plan)

    assert code == EXIT_ERROR
    captured = capsys.readouterr()
    out_lines = captured.out.strip("\n").splitlines()
    err_lines = captured.err.strip("\n").splitlines()
    assert len(out_lines) == 1, out_lines
    assert len(err_lines) == 1, err_lines
    assert out_lines[0] == err_lines[0]
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    return out_lines[0]


class TestExitCodeTwoCauses:
    def test_missing_spec_file(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--spec` не существует: код `2` (§2, §6)."""
        tree = _tree(wiring_repo)
        _write(wiring_repo, "plan.md", _plan(tree))

        message = _assert_exits_error_one_line(
            capsys, wiring_repo, spec="does-not-exist.md", plan="plan.md"
        )

        assert "does-not-exist.md" in message

    def test_undecodable_plan_file(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--plan` не декодируется как UTF-8: код `2` (§2, §6)."""
        _write(wiring_repo, "spec.md", _spec())
        (wiring_repo / "plan.md").write_bytes(b"\xff\xfe\x00 broken")

        message = _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

        assert "plan.md" in message

    def test_no_rule_block_in_spec(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ни одного блока `disputatio-wiring` в спеке: код `2` (§3.1, §6)."""
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", "# Спека без блока правил\n")
        _write(wiring_repo, "plan.md", _plan(tree))

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_broken_toml_in_rule_block(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Битый TOML внутри блока правил: код `2` (§3.1, §6)."""
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec('[[rule]\nid = "broken\n'))
        _write(wiring_repo, "plan.md", _plan(tree))

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_rule_schema_violation_missing_field(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Правилу не хватает обязательного поля: код `2` (§3.1, §6)."""
        tree = _tree(wiring_repo)
        bad_body = (
            '[[rule]]\nid = "p1-policy"\nkind = "construct-only-in"\n'
            'class = "pkg.policy:Policy"\n'
        )
        _write(wiring_repo, "spec.md", _spec(bad_body))
        _write(wiring_repo, "plan.md", _plan(tree))

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_rule_class_not_found_in_snapshot(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`class` правила не найден в снимке: код `2` (§3.2, §6)."""
        tree = _tree(wiring_repo)
        bad_body = (
            '[[rule]]\nid = "p1-policy"\nkind = "construct-only-in"\n'
            'class = "pkg.policy:NoSuchClass"\n'
            'allowed = ["src/pkg/composition.py"]\n'
        )
        _write(wiring_repo, "spec.md", _spec(bad_body))
        _write(wiring_repo, "plan.md", _plan(tree))

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_root_is_subdirectory_of_repository(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--root` — подкаталог репозитория: код `2`, а не пустой снимок (§6)."""
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        _write(wiring_repo, "plan.md", _plan(tree))

        message = _assert_exits_error_one_line(
            capsys,
            wiring_repo / SRC,
            spec=str(wiring_repo / "spec.md"),
            plan=str(wiring_repo / "plan.md"),
        )

        assert "не корень репозитория" in message

    def test_git_binary_missing(
        self,
        wiring_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`git` не найден в `PATH`: код `2` одной строкой, без traceback (§6)."""
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        _write(wiring_repo, "plan.md", _plan(tree))
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_duplicate_task_number_in_plan(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Два заголовка одной задачи в плане: код `2` (§5.3, §6)."""
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        plan_text = (
            _plan(tree)
            + "\n### Задача 1: первая\nтекст первой\n"
            + "\n### Задача 1: вторая\nтекст второй\n"
        )
        _write(wiring_repo, "plan.md", plan_text)

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_duplicate_task_number_with_nbsp_in_heading(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`### Task&nbsp;1:` — тоже заголовок задачи 1; дубль ловится (§5.3, §6)."""
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        plan_text = (
            _plan(tree)
            + "\n### Задача 1: первая\nтекст первой\n"
            + "\n### Задача 1: вторая\nтекст второй\n"
        )
        _write(wiring_repo, "plan.md", plan_text)

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_unparsable_python_file_under_src(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`.py` снимка не разбирается: код `2` (§4, §6)."""
        _write(wiring_repo, "src/pkg/broken.py", "def bad(:\n")
        _commit(wiring_repo, "add broken module")
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        _write(wiring_repo, "plan.md", _plan(tree))

        message = _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

        assert "broken.py" in message

    def test_star_import_of_snapshot_module(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`from <модуль снимка> import *`: код `2` (§3.4, §6)."""
        _write(wiring_repo, "src/pkg/star.py", "from pkg.policy import *\n")
        _commit(wiring_repo, "add star import")
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        _write(wiring_repo, "plan.md", _plan(tree))

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_relative_import_beyond_src_root(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Относительный импорт за корень `--src`: код `2` (§3.4, §6)."""
        _write(wiring_repo, "src/pkg/relimport.py", "from .. import policy\n")
        _commit(wiring_repo, "add relative import beyond root")
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec())
        _write(wiring_repo, "plan.md", _plan(tree))

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )

    def test_git_failure_when_root_is_not_a_repository(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--root` вне git-репозитория: сбой git даёт код `2` (§4, §6)."""
        _write(tmp_path, "spec.md", _spec())
        _write(tmp_path, "plan.md", _plan("0" * 40))

        _assert_exits_error_one_line(capsys, tmp_path, spec="spec.md", plan="plan.md")

    def test_code2_precedes_code1_broken_rules_with_dirty_src(
        self, wiring_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Битый блок правил ПРИ грязном `src` всё равно даёт код `2`, не `1` (§6).

        `dirty_src_paths` к этому моменту уже нашёл бы грязь, но
        `check_wiring` обязан поднять `WiringInputError` на разборе правил
        раньше, чем дойдёт до ветки `dirty-src` (design §6: «код 2 старше
        кода 1»).
        """
        tree = _tree(wiring_repo)
        _write(wiring_repo, "spec.md", _spec('[[rule]\nid = "broken\n'))
        _write(wiring_repo, "plan.md", _plan(tree))
        # Несохранённая правка делает `src` грязным, не трогая коммит.
        _write(wiring_repo, "src/pkg/policy.py", POLICY_SOURCE + "# dirty\n")

        _assert_exits_error_one_line(
            capsys, wiring_repo, spec="spec.md", plan="plan.md"
        )


# --- read-only и подключение через `run_gate`/`disp` (§2.1, §4) ----------------


def test_gate_writes_nothing(wiring_repo: Path) -> None:
    """Гейт read-only: `git status` не меняется, `.disputatio/` не появляется (§4)."""
    tree = _tree(wiring_repo)
    _write(wiring_repo, "spec.md", _spec())
    _write(wiring_repo, "plan.md", _plan(tree))
    before = _git(wiring_repo, "status", "--porcelain", "--ignored")

    _run_gate_cli(wiring_repo)

    after = _git(wiring_repo, "status", "--porcelain", "--ignored")
    assert after == before
    assert not (wiring_repo / ".disputatio").exists()


def _disp_entrypoint() -> Path:
    """Путь `disp` рядом с интерпретатором, гоняющим тесты (§2.1)."""
    disp = Path(sys.executable).parent / "disp"
    assert disp.is_file(), (
        f"{disp} не найден рядом с {sys.executable} — подключение §2.1 "
        "проверяется на entry point ТОЙ ЖЕ установки, что ведёт тесты; "
        "отсутствие файла — провал теста, а не `skip`"
    )
    return disp


def test_run_gate_wiring_passes_on_covered_tree(wiring_repo: Path) -> None:
    """`run_gate` через настоящий `disp`: покрытое дерево — `pass` (§2.1)."""
    disp = _disp_entrypoint()
    tree = _tree(wiring_repo)
    _write(wiring_repo, "spec.md", _spec())
    _write(wiring_repo, "plan.md", _plan(tree))
    cmd = shlex.join(
        [str(disp), "gate", "wiring", "--spec", "spec.md", "--plan", "plan.md"]
    )

    result = run_gate(GateSpec("wiring", cmd), wiring_repo)

    assert result.status is GateStatus.PASS
    assert result.exit_code == 0


def test_run_gate_wiring_fails_on_uncovered_tree(wiring_repo: Path) -> None:
    """`run_gate` через настоящий `disp`: непокрытое дерево — `fail` (§2.1)."""
    disp = _disp_entrypoint()
    _write(wiring_repo, "src/pkg/runner.py", RUNNER_SOURCE)
    _commit(wiring_repo, "add violation")
    tree = _tree(wiring_repo)
    _write(wiring_repo, "spec.md", _spec())
    _write(wiring_repo, "plan.md", _plan(tree))
    cmd = shlex.join(
        [str(disp), "gate", "wiring", "--spec", "spec.md", "--plan", "plan.md"]
    )

    result = run_gate(GateSpec("wiring", cmd), wiring_repo)

    assert result.status is GateStatus.FAIL
    assert result.exit_code is not None
    assert result.exit_code != 0
