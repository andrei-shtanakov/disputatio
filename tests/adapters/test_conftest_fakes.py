"""Тесты фикстуры `make_fake_process` — TASK-001, [DESIGN-008], [REQ-015].

Фикстура запрашивается через `request.getfixturevalue`, а не как параметр
теста: на red-чекпоинте `make_fake_process` ещё не существует, а lookup
через параметр — это pytest setup-error, не AssertionError, и red-гейт
такую ошибку не засчитывает (см. аналогичный паттерн в test_ports.py).
"""

import anyio
import pytest


def test_make_fake_process_yields_bytes_lines_wait_and_stderr(
    request: pytest.FixtureRequest,
) -> None:
    try:
        make_fake_process = request.getfixturevalue("make_fake_process")
    except Exception as exc:  # red-фаза: фикстуры ещё нет в conftest.py
        raise AssertionError(
            "tests/adapters/conftest.py ещё не реализует make_fake_process"
        ) from exc

    process = make_fake_process(["hello", "world"], stderr=b"warn", exit_code=2)

    async def collect_lines() -> list[bytes]:
        return [line async for line in process.stdout]

    lines = anyio.run(collect_lines)

    assert lines == [b"hello\n", b"world\n"]
    assert anyio.run(process.wait) == 2
    assert process.stderr == b"warn"
