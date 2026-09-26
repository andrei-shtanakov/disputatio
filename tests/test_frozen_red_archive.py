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
"""

import hashlib
from pathlib import Path
from typing import Final

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]

FROZEN_RED_ARCHIVE: Final[dict[str, str]] = {
    "tests/test_task_002_red.py": "c0b60e84bf1c31f8dd4a1e4ad4dd108701a6d41ee12391bfcb2a2ae024c02ce0",
    "tests/test_task_003_red.py": "085319123ce4ad5b8e6b495cf36e00cbe5c93f2db78691237e47f3ef296be51a",
    "tests/test_task_004_red.py": "c2a1bfe6666ea7ccc2c2e5b65f4ff9d39e48b658de6e146ddafc58bc287e0038",
    "tests/test_task_005_red.py": "7dcbc46cbb3c5a0bf593bfce8d3dcdd2e5f31dad2863915ea5c11a2292ef0431",
    "tests/test_ws57_task_001_red.py": "cc69ad508b1be6ee5f701f82402f38ddbbdba928148a2e257416cbea3743be43",
    "tests/test_ws57_task_002_red.py": "5a736a93c08d63eb04d5834a724409e2d9597e0366b1c9ebcc2690431534cc7a",
    "tests/test_ws57_task_005_red.py": "c67986fce8e19414e8519b82c7828d1a14f91badc73e210ee88f62a6fa045699",
    "tests/test_ws57_task_011_red.py": "931c74b9a239b49bcfb2e54442fa2c454d1d4c114b97e23779b39eef1aa707a8",
    "tests/test_ws57_task_014_red.py": "4cf25a6f19a0d649dbec8b7f0da5b5dedb7c44c2405b5b1db49a744182265cc5",
    "tests/test_ws57_task_015_red.py": "36bc6a991a7e8e27905bd58a2bcfc88a72d00a5471d1324c118cff2b85060bbc",
    "tests/verifier/test_task_001_red.py": "124da5d0d633cfec3f79bd55c05cb572fba16c213fd0513a430a8509dbd2041e",
    "tests/test_ws65_task_001_red.py": "1e6a927d004e0b957b7da437c307efa5f07e7dbe1c31b5ffe0d9e1edfd95726e",
}


#: Мощность пина — якорь, не выводимый из тех же списков (ревью #149): без
#: него опустошение пина вместе со строками `harness_files` давало бы сверку
#: пустых множеств, а `parametrize` по пустому набору — SKIPPED, не падение.
#: Меняется только вместе с архивом, отдельным PR.
FROZEN_RED_ARCHIVE_SIZE: Final = 12


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
