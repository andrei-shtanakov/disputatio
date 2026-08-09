"""Тесты TASK-002: `run_gate_command` — exit code именно проверяемого процесса.

Импорт `disputatio.verifier.capture` выполняется лениво внутри тестов через
`_import_capture`: на red-чекпоинте модуля ещё нет, и импорт на уровне
модуля сломал бы collection всего каталога. Хелпер пересобирает `ImportError`
в `AssertionError` — гейт принимает red только при падении assertion'ом.
"""

import dataclasses
import shlex
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _import_capture() -> ModuleType:
    """Импортирует `disputatio.verifier.capture`; отсутствие — `AssertionError`."""
    try:
        from disputatio.verifier import capture
    except ImportError as exc:  # red-фаза: capture.py ещё не создан
        raise AssertionError(
            "src/disputatio/verifier/capture.py ещё не создан"
        ) from exc
    return capture


def _python_cmd(code: str) -> str:
    """Собирает команду `<интерпретатор> -c <code>` с корректным quoting'ом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_exit_code_is_the_checked_process_code(tmp_path: Path) -> None:
    """Код 5 проверяемого процесса доходит до `RunOutcome.exit_code` как есть."""
    capture = _import_capture()

    outcome = capture.run_gate_command(_python_cmd("import sys; sys.exit(5)"), tmp_path)

    assert outcome.exit_code == 5


def test_noisy_failing_command_keeps_its_exit_code(tmp_path: Path) -> None:
    """Болтливая красная команда остаётся красной: хвост не подменяет код.

    Провокация [REQ-003]: если бы раннер захватывал вывод через собственный
    пайп (`| tail`, `| tee`), `exit_code` принадлежал бы зелёному хвосту.
    """
    capture = _import_capture()
    code = "import sys\nfor i in range(500):\n    print(f'line {i}')\nsys.exit(5)\n"

    outcome = capture.run_gate_command(_python_cmd(code), tmp_path)

    assert outcome.exit_code == 5
    assert "line 499" in outcome.tail


def test_shell_constructs_are_not_interpreted_by_the_runner(tmp_path: Path) -> None:
    """Раннер не оборачивает команду shell'ом: метасимволы доходят литералами."""
    capture = _import_capture()

    outcome = capture.run_gate_command(
        _python_cmd("import sys; print(sys.argv[1])") + " '$HOME | tail'", tmp_path
    )

    assert outcome.exit_code == 0
    assert "$HOME | tail" in outcome.tail


def test_declared_shell_pipeline_keeps_its_own_exit_semantics(tmp_path: Path) -> None:
    """`sh -c 'exit 5 | cat'`: возвращается код запущенного процесса (`sh`).

    Пайп заявлен самим конфигом, поэтому семантика exit code — на совести
    конфига: код пайплайна в `sh` равен коду последней команды (`cat`, 0).
    Раннер эту семантику не искажает и не извлекает «настоящую» 5.
    """
    capture = _import_capture()

    outcome = capture.run_gate_command("sh -c 'exit 5 | cat'", tmp_path)

    assert outcome.exit_code == 0


def test_successful_command_merges_stderr_into_the_captured_stream(
    tmp_path: Path,
) -> None:
    """Успешная команда даёт `exit_code == 0`; stderr попадает в общий хвост."""
    capture = _import_capture()
    code = (
        "import sys\n"
        "print('to-stdout')\n"
        "sys.stdout.flush()\n"
        "print('to-stderr', file=sys.stderr)\n"
    )

    outcome = capture.run_gate_command(_python_cmd(code), tmp_path)

    assert outcome.exit_code == 0
    assert "to-stdout" in outcome.tail
    assert "to-stderr" in outcome.tail


def test_non_utf8_output_does_not_break_capture(tmp_path: Path) -> None:
    """Не-UTF-8 байты не роняют захват: `errors="replace"` вместо исключения."""
    capture = _import_capture()
    code = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe binary\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )

    outcome = capture.run_gate_command(_python_cmd(code), tmp_path)

    assert outcome.exit_code == 0
    assert "�" in outcome.tail
    assert "binary" in outcome.tail


def test_command_runs_in_the_given_workdir(tmp_path: Path) -> None:
    """Процесс стартует в `workdir`, а не в каталоге раннера."""
    capture = _import_capture()
    workdir = tmp_path / "gate-workdir"
    workdir.mkdir()

    outcome = capture.run_gate_command(
        _python_cmd("import os; print(os.getcwd())"), workdir
    )

    assert outcome.exit_code == 0
    assert Path(outcome.tail.strip()).resolve() == workdir.resolve()


def test_run_outcome_is_frozen_dataclass_with_declared_fields(tmp_path: Path) -> None:
    """`RunOutcome` — frozen dataclass с полями исхода запуска одного gate."""
    capture = _import_capture()

    outcome = capture.run_gate_command(_python_cmd("pass"), tmp_path)

    assert [f.name for f in dataclasses.fields(outcome)] == [
        "exit_code",
        "tail",
        "duration_s",
        "error",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.exit_code = 0  # type: ignore[read-only]


def test_verifier_sources_are_free_of_agent_cli_invocations() -> None:
    """[REQ-010]/INV-10: в исходниках Verifier'а нет агентских CLI.

    Точка запуска процессов появилась (`capture.py`) — значит проверка
    неагентности перестала быть вакуумной и должна выполняться по всем
    модулям пакета.
    """
    repo_root = Path(__file__).resolve().parents[2]
    package_dir = repo_root / "src" / "disputatio" / "verifier"
    sources = sorted(package_dir.glob("*.py"))

    assert (package_dir / "capture.py") in sources, "capture.py ещё не создан"

    forbidden = ("claude", "codex", "aider", "cursor-agent", "gemini")
    for source in sources:
        text = source.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{source.name}: агентский CLI {token}"
