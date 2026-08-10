"""Регистрация консольной команды `disp` ([REQ-021], [DESIGN-021], [TASK-022]).

Установленный бинарь тут намеренно не запускается: он есть ровно тогда, когда
пакет установлен в это окружение, и набор, зеленеющий от `uv sync`, доказывал
бы состояние `.venv`, а не содержимое репозитория. Предмет проверки —
объявление: `pyproject.toml` парсится `tomllib`, строка `"модуль:атрибут"`
разбирается, модуль импортируется `importlib.import_module`, атрибут
проверяется на `callable`.

Отсюда три решения:

* **Модуль сверяется с `disputatio.cli` буквально.** Без этого объявление,
  указывающее на любой импортируемый модуль с callable-атрибутом (`json:dumps`
  разбирается и импортируется не хуже), прошло бы проверку целиком.
* **`--help` вызывается у того callable, который объявлен в `pyproject.toml`**,
  а не у импортированного отдельно `cli.main`: иначе тест пинил бы функцию, до
  которой команда `disp` может и не доводить.
* **Оба процессных шва заминированы на всё время вызова** — `subprocess.run`
  (по нему ходит `runtime.git`) и `asyncio.create_subprocess_exec` (по нему
  ходят адаптеры). «Сессия не стартует» — это утверждение о том, что наружу не
  ушло ни одного процесса; проверка одного лишь отсутствия `.disputatio/`
  оставляла бы pre-flight незамеченным.
"""

import asyncio
import subprocess
import tomllib
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

_PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"
_ENTRYPOINT_NAME = "disp"
_EXPECTED_MODULE = "disputatio.cli"


def _entrypoint_value() -> str:
    """Строка объявления `disp` из `[project.scripts]`."""
    with _PYPROJECT.open("rb") as fh:
        config = tomllib.load(fh)
    project = config["project"]
    assert "scripts" in project, "в pyproject.toml нет секции [project.scripts]"
    scripts = project["scripts"]
    assert _ENTRYPOINT_NAME in scripts, (
        f"[project.scripts] не объявляет {_ENTRYPOINT_NAME!r}: {sorted(scripts)}"
    )
    value = scripts[_ENTRYPOINT_NAME]
    assert isinstance(value, str)
    return value


def _entrypoint_callable() -> Any:
    """Callable, на который указывает объявление `disp`."""
    value = _entrypoint_value()
    module_name, separator, attribute = value.partition(":")
    assert separator == ":", f"объявление {value!r} не имеет вида модуль:атрибут"
    assert module_name == _EXPECTED_MODULE, (
        f"entrypoint указывает на {module_name!r}, а не на {_EXPECTED_MODULE!r}"
    )
    assert attribute, f"объявление {value!r} не называет атрибут"
    module = import_module(module_name)
    assert hasattr(module, attribute), (
        f"в {module_name} нет атрибута {attribute!r}: {sorted(vars(module))}"
    )
    target = getattr(module, attribute)
    assert callable(target), f"{value} не callable: {target!r}"
    return target


def _mine_process_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заминировать оба шва порождения процесса."""

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"наружу ушёл процесс: {args!r}")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", explode)


def test_pyproject_declares_disp_pointing_at_a_callable_in_disputatio_cli() -> None:
    """`[project.scripts] disp` разрешается в callable из `disputatio.cli`."""
    assert _entrypoint_callable() is not None


def test_declared_entrypoint_answers_help_without_starting_a_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main(["--help"])` → `SystemExit(0)`, и ни процесса, ни `.disputatio/`."""
    entrypoint = _entrypoint_callable()
    monkeypatch.chdir(tmp_path)
    _mine_process_seams(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        entrypoint(["--help"])

    assert excinfo.value.code == 0
    stdout = capsys.readouterr().out
    assert "run" in stdout and "resume" in stdout
    assert not (tmp_path / ".disputatio").exists()
    assert list(tmp_path.iterdir()) == []
