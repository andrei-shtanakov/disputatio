"""Инвариант «адаптеры не исполняют verification gates»: TASK-009, [DESIGN-009],
[REQ-010].

Сканер (`.gate_scan`) обходит модули `disputatio.adapters` через `ast.parse`
их исходников — читает pytest, а не сам пакет. Импорт сканера обёрнут: на
момент red-чекпоинта модуля ещё нет, и импорт на уровне теста сломал бы
collection. Red-селектор (`test_adapters_do_not_import_verifier`) превращает
`ImportError` в `AssertionError` — гейт принимает red только при падении
assertion'ом.

Известное ограничение (принимается сознательно, по образцу ast-scan из w-fsm
TASK-003 и прямо зафиксировано в [DESIGN-009]): проверка чисто синтаксическая.
`__import__("disputatio.contracts.ports")`, `importlib.import_module(...)`,
`getattr(ports, "Veri" + "fier")` и построение argv из переменной, а не
литерала, её обходят. Закрывать эти дыры в TASK-009 не пытаемся: acceptance
REQ-010 требует именно import-инварианта, который точен и однозначен, а
argv-проверка объявлена эвристической.
"""

from pathlib import Path
from types import ModuleType


def _gate_scan() -> ModuleType:
    try:
        from . import gate_scan
    except ImportError as exc:  # red-фаза: tests/adapters/gate_scan.py ещё нет
        raise AssertionError("tests/adapters/gate_scan.py ещё не создан") from exc
    return gate_scan


def test_adapters_do_not_import_verifier() -> None:
    """[REQ-010]: ни один модуль пакета не тянет `Verifier` из contracts."""
    scan = _gate_scan()

    package_dir = scan.adapters_package_dir()
    scanned = {path.name for path in scan.iter_module_paths(package_dir)}

    # Не-вакуумность: обход действительно видит все модули TASK-002…008.
    assert scanned >= {
        "__init__.py",
        "_process.py",
        "claude_code.py",
        "codex.py",
        "events.py",
        "fallback.py",
        "permissions.py",
    }
    assert scan.scan_verifier_usage(package_dir) == {}


def test_adapters_do_not_invoke_gate_binaries_directly() -> None:
    """[REQ-010]: `pytest`/`ruff` нигде не захардкожены как `argv[0]`."""
    scan = _gate_scan()

    assert scan.scan_gate_argv(scan.adapters_package_dir()) == {}


def test_find_verifier_usage_detects_direct_port_import() -> None:
    """Негативный самотест: `Verifier` рядом с легальными именами из `ports`."""
    scan = _gate_scan()

    source = "from disputatio.contracts.ports import AgentTurn, Verifier\n"

    assert scan.find_verifier_usage(source) == ["disputatio.contracts.ports.Verifier"]


def test_find_verifier_usage_detects_reexport_and_attribute_access() -> None:
    """Обход через re-export пакета и через атрибут модуля тоже ловится."""
    scan = _gate_scan()

    source = (
        "from disputatio.contracts import Verifier\n"
        "import disputatio.contracts.ports as ports\n\n\n"
        "def gate(v: Verifier) -> object:\n"
        "    return ports.Verifier\n"
    )

    assert scan.find_verifier_usage(source) == [
        "disputatio.contracts.Verifier",
        "ports.Verifier",
    ]


def test_find_verifier_usage_allows_legitimate_ports_imports() -> None:
    """Соседние протоколы из того же модуля — не нарушение (иначе тест вакуумен)."""
    scan = _gate_scan()

    source = (
        "from disputatio.contracts.base import Role\n"
        "from disputatio.contracts.ports import AgentAdapter, AgentTurn, EventSink\n"
    )

    assert scan.find_verifier_usage(source) == []


def test_find_gate_argv_detects_literal_first_element() -> None:
    """Эвристика: литерал списка/кортежа, начинающийся с имени гейта."""
    scan = _gate_scan()

    source = "PYTEST_ARGV = ['pytest', '-q']\nRUFF_ARGV = ('ruff', 'check', '.')\n"

    assert scan.find_gate_argv(source) == ["pytest", "ruff"]


def test_find_gate_argv_detects_call_first_argument_and_abs_path() -> None:
    """`launcher("ruff", ...)` и абсолютный путь до бинаря — тоже `argv[0]`."""
    scan = _gate_scan()

    source = (
        "async def run(launcher):\n"
        "    await launcher('ruff', 'check', '.')\n"
        "    return subprocess.run(['/usr/local/bin/pytest', '-q'])\n"
    )

    assert scan.find_gate_argv(source) == ["pytest", "ruff"]


def test_find_gate_argv_exempts_reviewer_allowed_tools_literal() -> None:
    """Освобождение адресное: тот же литерал под другим именем — нарушение."""
    scan = _gate_scan()

    source = (
        "REVIEWER_DEFAULT_ALLOWED_TOOLS = ('pytest', 'ruff')\n"
        "OTHER_ARGV = ('pytest', '-q')\n"
    )

    assert scan.find_gate_argv(source) == ["pytest"]


def test_find_gate_argv_allows_bash_permission_literals() -> None:
    """`Bash(pytest:*)` — permission-литерал, а не вызов гейта."""
    scan = _gate_scan()

    source = "TOOLS = ('Bash(pytest:*)', 'Bash(ruff:*)', 'Read')\n"

    assert scan.find_gate_argv(source) == []


def test_scans_discover_new_modules_dynamically(tmp_path: Path) -> None:
    """Новый (в т.ч. вложенный) модуль каталога не выпадает из обхода."""
    scan = _gate_scan()

    (tmp_path / "clean.py").write_text(
        "from disputatio.contracts.ports import AgentTurn\n", encoding="utf-8"
    )
    (tmp_path / "rogue.py").write_text(
        "from disputatio.contracts.ports import Verifier\n", encoding="utf-8"
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "deep.py").write_text("ARGV = ['pytest', '-q']\n", encoding="utf-8")

    assert scan.scan_verifier_usage(tmp_path) == {
        "rogue.py": ["disputatio.contracts.ports.Verifier"]
    }
    assert scan.scan_gate_argv(tmp_path) == {"nested/deep.py": ["pytest"]}
