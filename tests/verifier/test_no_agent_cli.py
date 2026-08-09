"""Отсутствие агентских CLI в исходниках Verifier'а (TASK-010, [REQ-010]).

Инвариант INV-10: Verifier исполняет только детерминированные команды из
конфигурации gates, поэтому в `src/disputatio/verifier/**` не должно быть ни
упоминаний агентских CLI (`claude`, `codex` и т.п.), ни импортов вне stdlib и
`disputatio.contracts`/`disputatio.verifier`. Проверка статическая: исходники
читаются как текст и как AST, ничего не импортируется и не запускается.

Сам сканер живёт в `.agent_cli_scanner` — не-тестовом модуле рядом (как
`.mutation_probe` у TASK-009). Импорт обёрнут: на момент red-чекпоинта модуля
ещё нет, и импорт на уровне теста сломал бы collection всего каталога. Хелпер
пересобирает `ImportError` в `AssertionError` — гейт принимает red только при
падении assertion'ом.
"""

from pathlib import Path
from types import ModuleType


def _scanner() -> ModuleType:
    """Сканер `.agent_cli_scanner`; на red-фазе его отсутствие — assertion."""
    try:
        from . import agent_cli_scanner
    except ImportError as exc:  # red-фаза: сканера ещё нет
        raise AssertionError(
            "tests/verifier/agent_cli_scanner.py ещё не создан"
        ) from exc
    return agent_cli_scanner


def test_check_no_agent_cli_discovers_rogue_module_in_subpackage(
    tmp_path: Path,
) -> None:
    """Обход динамический и рекурсивный: новый модуль и под-пакет не выпадают."""
    checker = _scanner()

    (tmp_path / "rogue.py").write_text(
        'import subprocess\n\n\ndef ask():\n    subprocess.run(["claude", "-p"])\n',
        encoding="utf-8",
    )
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_text(
        '_ARGV = ("codex", "exec")\n', encoding="utf-8"
    )
    (tmp_path / "clean.py").write_text("import shlex\n", encoding="utf-8")

    violations = checker.check_no_agent_cli(tmp_path)

    assert violations == {"rogue.py": ["claude"], "sub/inner.py": ["codex"]}


def test_scan_source_detects_agent_cli_in_comments_case_insensitively() -> None:
    """Упоминание ловится в комментарии и в строке, в любом регистре."""
    checker = _scanner()

    source = (
        '# TODO: дёрнуть Claude Code вместо gate\nHINT = "CODEX exec --full-auto"\n'
    )

    assert checker.scan_source(source) == ["claude", "codex"]


def test_forbidden_tokens_cover_cli_named_by_requirement() -> None:
    """[REQ-010] называет `claude` и `codex` поимённо — они обязаны быть в списке."""
    checker = _scanner()

    assert {"claude", "codex"} <= checker.FORBIDDEN_CLI_TOKENS


def test_scan_source_allows_stdlib_and_contracts_imports() -> None:
    """Allowlist [REQ-010]: stdlib (включая `subprocess`) + `disputatio.*`-пакеты."""
    checker = _scanner()

    source = (
        "import shlex\n"
        "import subprocess\n"
        "from collections import deque\n"
        "from dataclasses import dataclass\n"
        "from pathlib import Path\n"
        "from typing import Final\n"
        "from disputatio.contracts.verification import GateResult\n"
        "from disputatio.verifier.config import GateSpec\n"
    )

    assert checker.scan_source(source) == []


def test_scan_source_flags_third_party_import_even_if_installed() -> None:
    """Критерий — stdlib, а не «пакет установлен»: `pydantic` тоже нарушение."""
    checker = _scanner()

    source = "import pydantic\nfrom httpx import Client\n"

    assert checker.scan_source(source) == ["pydantic", "httpx"]


def test_scan_source_flags_relative_imports() -> None:
    """Относительный импорт скрывает цель от allowlist'а по имени — нарушение."""
    checker = _scanner()

    source = "from . import capture\nfrom .config import GateSpec\n"

    assert checker.scan_source(source) == [".capture", ".config"]


def test_scan_source_flags_dynamic_import_calls() -> None:
    """`__import__`/`importlib.import_module` обходят allowlist — тоже нарушение."""
    checker = _scanner()

    source = (
        "import importlib\n\n\n"
        "def load(name):\n"
        "    return importlib.import_module(name), __import__(name)\n"
    )

    assert sorted(checker.scan_source(source)) == ["__import__", "import_module"]


def test_scan_source_ignores_legitimate_verifier_vocabulary() -> None:
    """Список токенов не размашист: `git`, `code`, `cursor` — не нарушения."""
    checker = _scanner()

    source = (
        '"""Раннер gates: git diff --numstat HEAD, exit code процесса."""\n'
        "import subprocess\n\n\n"
        "def run(argv):\n"
        "    cursor = 0  # позиция чтения в потоке\n"
        "    return subprocess.run(argv, shell=False).returncode + cursor\n"
    )

    assert checker.scan_source(source) == []


def test_iter_module_paths_covers_actual_package_modules() -> None:
    """Обход указывает на реальный пакет и не фильтрует его модули."""
    checker = _scanner()

    names = {
        path.name for path in checker.iter_module_paths(checker.verifier_package_dir())
    }

    assert {"__init__.py", "capture.py", "runner.py", "diffstats.py"} <= names


def test_actual_verifier_package_is_free_of_agent_cli() -> None:
    """Green [REQ-010]: фактический `disputatio.verifier` проходит скан чисто."""
    checker = _scanner()

    assert checker.check_no_agent_cli(checker.verifier_package_dir()) == {}
