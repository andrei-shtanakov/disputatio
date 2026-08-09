"""Тесты TASK-003: bounded tail — кольцевое ограничение вывода ([DESIGN-005]).

[REQ-005] задаёт три наблюдаемых случая (длиннее лимита → последние N строк,
короче лимита → весь вывод, без вывода → `""`), NFR-004 — способ их
достижения: построчное чтение, при котором полный вывод не удерживается в
памяти целиком. Первое проверяется настоящими процессами, второе — фейковым
процессом, чей поток запрещает `read()`/`readlines()`, и замером пиковой
аллокации на «болтливом» выводе.

Импорт `disputatio.verifier.capture` ленив, а отсутствие `tail_lines`/
`DEFAULT_TAIL_LINES` пересобирается в `AssertionError` (через проверку
сигнатуры, а не перехват `TypeError`, который замаскировал бы настоящий
`TypeError` из тела функции): гейт принимает red только при падении
assertion'ом.
"""

import inspect
import shlex
import subprocess
import sys
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, Self

import pytest

# Столько строк по столько символов печатает «болтливый» фейковый gate в
# тесте пиковой памяти: 10 МБ вывода против порога в 1 МБ — накопление
# полного вывода в строке-буфере промахивается мимо порога на порядок.
_NOISY_LINES = 50_000
_NOISY_LINE_WIDTH = 200
_PEAK_MEMORY_LIMIT_BYTES = 1 << 20


def _import_capture() -> ModuleType:
    """Импортирует `disputatio.verifier.capture`; отсутствие — `AssertionError`."""
    try:
        from disputatio.verifier import capture
    except ImportError as exc:  # red-фаза: capture.py ещё не создан
        raise AssertionError(
            "src/disputatio/verifier/capture.py ещё не создан"
        ) from exc
    return capture


def _tail_lines_parameter(capture: ModuleType) -> inspect.Parameter:
    """Возвращает параметр `tail_lines` из сигнатуры `run_gate_command`."""
    parameters = inspect.signature(capture.run_gate_command).parameters
    parameter = parameters.get("tail_lines")
    assert parameter is not None, "run_gate_command ещё не принимает tail_lines"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
        "tail_lines должен быть keyword-only параметром"
    )
    return parameter


def _run_bounded(
    capture: ModuleType, cmd: str, workdir: Path, *, tail_lines: int
) -> Any:
    """Вызывает `run_gate_command` с явным лимитом, проверив его наличие."""
    _tail_lines_parameter(capture)
    return capture.run_gate_command(cmd, workdir, tail_lines=tail_lines)


def _python_cmd(code: str) -> str:
    """Собирает команду `<интерпретатор> -c <code>` с корректным quoting'ом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _printing_cmd(count: int, start: int = 0) -> str:
    """Команда, печатающая `count` строк вида `line N`, начиная с `start`."""
    return _python_cmd(
        f"for i in range({start}, {start + count}):\n    print(f'line {{i}}')\n"
    )


class _LineStream:
    """Поток фейкового процесса: отдаёт строки по одной, `read()` запрещён.

    Построчное чтение (итерация или `readline`) разрешено и учитывается —
    `max_chars_per_call` фиксирует, сколько символов раннер забрал за один
    вызов; `read`/`readlines` отдали бы весь вывод разом и падают.
    """

    def __init__(self, lines: Iterator[str]) -> None:
        self._lines = lines
        self.max_chars_per_call = 0

    def read(self, *args: object) -> str:
        raise AssertionError("run_gate_command забрал весь вывод одним read()")

    def readlines(self, *args: object) -> list[str]:
        raise AssertionError("run_gate_command забрал весь вывод одним readlines()")

    def readline(self, *args: object) -> str:
        return self._record(next(self._lines, ""))

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> str:
        return self._record(next(self._lines))

    def _record(self, chunk: str) -> str:
        self.max_chars_per_call = max(self.max_chars_per_call, len(chunk))
        return chunk


class _FakeProcess:
    """Мок `Popen`: контекст-менеджер с потоком `stdout` и кодом возврата."""

    def __init__(self, stdout: _LineStream, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def wait(self) -> int:
        return self.returncode


def _lazy_lines(count: int, width: int) -> Iterator[str]:
    """Ленивый генератор `count` строк по `width` символов (плюс `\\n`)."""
    for i in range(count):
        yield f"{i:0{width}d}\n"


def _patch_popen(monkeypatch: pytest.MonkeyPatch, stream: _LineStream) -> None:
    """Подменяет `subprocess.Popen` фейковым процессом с потоком `stream`."""

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        return _FakeProcess(stream)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)


def test_tail_keeps_only_the_last_lines_when_output_exceeds_the_limit(
    tmp_path: Path,
) -> None:
    """Вывод длиннее лимита → в `tail` только последние N строк ([REQ-005])."""
    capture = _import_capture()

    outcome = _run_bounded(capture, _printing_cmd(10), tmp_path, tail_lines=3)

    assert outcome.tail.splitlines() == ["line 7", "line 8", "line 9"]
    assert "line 6" not in outcome.tail


def test_tail_keeps_the_whole_output_when_it_is_shorter_than_the_limit(
    tmp_path: Path,
) -> None:
    """Вывод короче лимита попадает в `tail` целиком ([REQ-005])."""
    capture = _import_capture()

    outcome = _run_bounded(capture, _printing_cmd(2), tmp_path, tail_lines=5)

    assert outcome.tail.splitlines() == ["line 0", "line 1"]


def test_tail_is_empty_when_the_gate_prints_nothing(tmp_path: Path) -> None:
    """Gate без вывода → `tail == ""`, а не перевод строки ([REQ-005])."""
    capture = _import_capture()

    outcome = _run_bounded(capture, _python_cmd("pass"), tmp_path, tail_lines=5)

    assert outcome.tail == ""


def test_default_limit_is_the_declared_constant_and_applies_without_argument(
    tmp_path: Path,
) -> None:
    """Дефолт лимита — `DEFAULT_TAIL_LINES`, и он действует без аргумента."""
    capture = _import_capture()

    default = getattr(capture, "DEFAULT_TAIL_LINES", None)
    assert default == 200, "capture.DEFAULT_TAIL_LINES ещё не объявлен ([DESIGN-005])"
    assert _tail_lines_parameter(capture).default == default

    outcome = capture.run_gate_command(_printing_cmd(250), tmp_path)

    lines = outcome.tail.splitlines()
    assert len(lines) == default
    assert lines[0] == "line 50"
    assert lines[-1] == "line 249"


def test_output_is_read_line_by_line_without_a_whole_output_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Чтение построчное: `read()`/`readlines()` не вызываются вовсе."""
    capture = _import_capture()
    stream = _LineStream(_lazy_lines(_NOISY_LINES, _NOISY_LINE_WIDTH))
    _patch_popen(monkeypatch, stream)

    outcome = _run_bounded(capture, "noisy-gate", tmp_path, tail_lines=3)

    assert stream.max_chars_per_call == _NOISY_LINE_WIDTH + 1
    assert outcome.tail.splitlines() == [
        f"{i:0{_NOISY_LINE_WIDTH}d}" for i in range(_NOISY_LINES - 3, _NOISY_LINES)
    ]


def test_peak_memory_does_not_grow_with_the_size_of_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-004: пик аллокаций не масштабируется с объёмом вывода.

    Фейковый процесс печатает ~10 МБ; строка-буфер (`read()`, `+=`,
    `"".join(all_lines)`) удержала бы их целиком и пробила бы порог в 1 МБ,
    кольцевой буфер из трёх строк — нет.
    """
    capture = _import_capture()
    stream = _LineStream(_lazy_lines(_NOISY_LINES, _NOISY_LINE_WIDTH))
    _patch_popen(monkeypatch, stream)

    tracemalloc.start()
    try:
        _run_bounded(capture, "noisy-gate", tmp_path, tail_lines=3)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < _PEAK_MEMORY_LIMIT_BYTES, (
        f"пик {peak} байт при выводе "
        f"~{_NOISY_LINES * (_NOISY_LINE_WIDTH + 1)} байт — вывод накапливается"
    )
