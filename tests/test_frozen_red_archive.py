"""RED-тесты закрытых волн в корне `tests/` неизменны (пин sha256, ревью #149).

Scope workstream'а пускает запись под `tests/test_*_red.py` — там spec-runner
кладёт RED-тест волны (`tdd_runners.py::evidential_file`), — и этот глоб
захватывает 11 RED-тестов прошлых волн; двенадцатый архивный,
`tests/verifier/test_task_001_red.py` прогона 2026-08-22, входит в scope через
`tests/verifier/**`. Их защищают два независимых слоя:

- `harness_guard: strict` (файлы поимённо в `harness_files`): в пределах одного
  процесса spec-runner база снимается один раз на задачу (`HarnessBaseline`,
  spec-runner#137), и правка отвергается на каждой попытке. Новый процесс —
  повтор workstream'а maestro или `--resume` — снимает базу заново;
- этот тест: он входит в `uv run pytest -q`, приёмку каждой задачи, и краснеет
  на любой правке или удалении файла — независимо от того, какой процесс и с
  какой базой её пропустил.

Сам пин лежит в корне `tests/` под именем вне глоба `tests/test_*_red.py`, то
есть вне scope workstream'а, и тоже перечислен в `harness_files`.

Легитимная правка архивного файла — только отдельным PR с обновлением пина.

Исполнение сторожит третий слой (ревью #151, #152): хук корневого
`conftest.py` в прогоне, собирающем архивный файл, требует, чтобы тест был
собран и прошёл (`frozen_red_archive`). Он ловит **неумышленное** снятие теста
с коллекции или пропуск через соседний `tests/verifier/conftest.py` (он в scope
и не заморожен). Умышленное выключение из того же conftest'а (фильтр в
`config.option`, переписанный `exitstatus`) слой обойдёт: как и пин, это
аварийная компенсация, а не граница доверия.
"""

import hashlib
from pathlib import Path

import pytest
import yaml
from frozen_red_archive import (
    FROZEN_RED_ARCHIVE,
    FROZEN_RED_ARCHIVE_SIZE,
    archive_violations,
    covered_archive,
)

_ROOT = Path(__file__).resolve().parents[1]


def test_archive_has_its_declared_size() -> None:
    """Пин не опустошён и не сокращён молча: мощность — независимый якорь."""
    assert len(FROZEN_RED_ARCHIVE) == FROZEN_RED_ARCHIVE_SIZE


@pytest.mark.parametrize("path", sorted(FROZEN_RED_ARCHIVE))
def test_frozen_red_test_is_unchanged(path: str) -> None:
    """Файл существует и его байты совпадают с пином."""
    data = (_ROOT / path).read_bytes()

    assert hashlib.sha256(data).hexdigest() == FROZEN_RED_ARCHIVE[path]


def test_archive_list_matches_harness_files() -> None:
    """Пин и `harness_files` называют одни и те же архивные RED-тесты."""
    config = yaml.safe_load((_ROOT / "project.yaml").read_text(encoding="utf-8"))
    harness = config["spec_runner"]["extra_executor_config"]["executor"][
        "harness_files"
    ]
    archived = {item for item in harness if item.endswith("_red.py")}

    assert archived == set(FROZEN_RED_ARCHIVE)


def test_archive_violations_accepts_all_passed() -> None:
    """Все архивные файлы прошли — нарушений нет."""
    outcomes = {path: ["passed"] for path in FROZEN_RED_ARCHIVE}

    assert archive_violations(outcomes) == []


def test_archive_violations_flags_uncollected_file() -> None:
    """Файл без отчётов (снят `collect_ignore`) — нарушение."""
    outcomes = {path: ["passed"] for path in FROZEN_RED_ARCHIVE}
    del outcomes["tests/verifier/test_task_001_red.py"]

    assert archive_violations(outcomes) == [
        "tests/verifier/test_task_001_red.py: не собран или не исполнен"
    ]


def test_archive_violations_flags_skipped_file() -> None:
    """Пропуск skip-фикстурой — нарушение, даже без `passed`-соседей."""
    outcomes = {path: ["passed"] for path in FROZEN_RED_ARCHIVE}
    outcomes["tests/verifier/test_task_001_red.py"] = ["skipped"]

    assert archive_violations(outcomes) == [
        "tests/verifier/test_task_001_red.py: не собран или не исполнен"
    ]


def test_archive_violations_checks_only_the_given_paths() -> None:
    """Непокрытый прогоном файл нарушением не считается."""
    outcomes: dict[str, list[str]] = {}

    assert archive_violations(outcomes, paths=()) == []


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # Форма `config.args` полного `pytest -q` — абсолютный каталог запуска
        # (pytest подставляет его сам, ArgsSource.INVOCATION_DIR).
        ([str(_ROOT)], set(FROZEN_RED_ARCHIVE)),
        (["."], set(FROZEN_RED_ARCHIVE)),
        (["tests"], set(FROZEN_RED_ARCHIVE)),
        (["tests/verifier"], {"tests/verifier/test_task_001_red.py"}),
        (["tests/runtime"], set()),
        (
            ["tests/verifier/test_task_001_red.py::test_x"],
            set(),
        ),
    ],
    ids=["invocation-dir", "dot", "tests", "verifier", "unrelated", "node-id"],
)
def test_covered_archive_follows_the_run_selection(
    args: list[str], expected: set[str]
) -> None:
    """Покрытие — по путям запуска, а не по «аргументы не заданы» (ревью #152)."""
    assert covered_archive(_ROOT, _ROOT, args) == expected
