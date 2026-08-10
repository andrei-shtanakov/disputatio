"""Типизированные операторские remedy: `abandon`, `repair`, кросс-лок, lint на red.

Закрывает четыре issue пилота, которые оказались одним дефектом — у гейта не
было операторского remedy, и каждая ошибка в зафиксированном тесте оплачивалась
переписыванием истории:

- disputatio#12 — удаление claim'а не работало: recovery восстанавливал его из
  red-коммита по трейлерам и снова запирал тот же негодный тест;
- disputatio#13 — `verify` смотрел только файл текущего селектора, поэтому
  byte-lock чужих тестов держался конституцией агента, а не прибором;
- disputatio#8 — remedy-подсказка советовала недействующее («удалите claim»);
- disputatio#7 — `red` не проверял lint фиксируемого файла, и замок
  консервировал долг.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate

from .conftest import write_tasks

SELECTOR = "tests/test_new.py::test_x"
TEST_PATH = SELECTOR.split("::")[0]
OTHER_SELECTOR = "tests/test_other.py::test_y"
OTHER_PATH = OTHER_SELECTOR.split("::")[0]

ONE_RUNNING = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
"""

TWO_TASKS_SECOND_RUNNING = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | ✅ DONE
### TASK-002: Вторая
- Приоритет: P1 | 🔄 IN_PROGRESS
"""


@pytest.fixture(autouse=True)
def _local_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Локальные pytest и ruff: tmp-репо фикстуры не uv-проект."""
    monkeypatch.setattr(tdd_gate, "PYTEST_CMD", (sys.executable, "-m", "pytest", "-q"))
    monkeypatch.setattr(tdd_gate, "RUFF_CMD", (sys.executable, "-m", "ruff"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True)


def _write_failing(repo: Path, path: str = TEST_PATH, name: str = "test_x") -> None:
    """Тест, красный на baseline и lint-чистый.

    Красный по отсутствию реализации, а не по `assert False`: green обязан
    достигаться продуктовым кодом, а тест — оставаться байт-неизменным, и
    хелпер, переписывающий тест ради зелёного, проверял бы не гейт, а обход
    гейта.
    """
    marker = f"READY-{name}"
    (repo / path).write_text(
        "from pathlib import Path\n\n\n"
        f"def {name}():\n"
        f'    assert Path("src/mod.py").read_text().strip() == "{marker}"\n',
        encoding="utf-8",
    )


def _implement(repo: Path, name: str = "test_x") -> None:
    """Реализация, закрывающая `_write_failing`, отдельным коммитом."""
    (repo / "src" / "mod.py").write_text(f"READY-{name}\n", encoding="utf-8")
    tdd_gate.git(repo, "add", "--", "src/mod.py")
    tdd_gate.git(repo, "commit", "-q", "-m", f"impl: {name}")


def _runner_auto_commit(repo: Path) -> None:
    """Коммитит evidence так, как это делает раннер хуком `post_done`.

    Сам гейт claim'ы и вердикты не коммитит (`commit_red` пишет только
    `tests/`), поэтому без этого шага следующая задача упирается в «запрещённые
    правки до red»: незакоммиченная evidence предыдущей задачи выглядит как
    посторонняя правка рабочего дерева.
    """
    tdd_gate.git(repo, "add", "-A", "--", "spec/.tdd-evidence")
    tdd_gate.git(repo, "commit", "-q", "-m", "evidence")


def _red(repo: Path, selector: str = SELECTOR) -> int:
    return tdd_gate.cmd_red(repo, selector, "поведение")


# --- disputatio#7: lint фиксируемого файла ----------------------------------


def test_red_refuses_a_test_file_that_does_not_pass_lint(repo: Path) -> None:
    """Грязный по lint тест не попадает под замок — иначе долг неисправим.

    Замок делает файл байт-неизменяемым, поэтому lint-ошибка, попавшая в
    red-чекпоинт, не чинится вообще ничем, кроме операторского вмешательства,
    и бьёт по каждой следующей задаче того же suite.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    (repo / TEST_PATH).write_text(
        # I001: порядок импортов — ровно та ловушка, что сработала трижды.
        "import sys\nimport os\n\n\ndef test_x():\n    assert False, 'nope'\n",
        encoding="utf-8",
    )

    code = _red(repo)

    assert code == 1
    assert tdd_gate.load_claim(repo, "TASK-001", "default") is None, (
        "claim не должен появиться: чекпоинт не состоялся"
    )


def test_red_accepts_a_lint_clean_failing_test(repo: Path) -> None:
    """Контроль к предыдущему: чистый падающий тест проходит как раньше."""
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)

    assert _red(repo) == 0
    assert tdd_gate.load_claim(repo, "TASK-001", "default") is not None


# --- disputatio#12: abandon разрывает круг recovery -------------------------


def test_deleting_the_claim_alone_is_not_a_remedy(repo: Path) -> None:
    """Документирует саму дыру: recovery восстанавливает удалённый claim.

    Тест закрепляет НЕЖЕЛАТЕЛЬНОЕ, но реальное поведение — оно и есть причина
    существования `abandon`. Если однажды recovery перестанет восстанавливать
    claim без явного отказа, этот тест упадёт и заставит перечитать #12.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0
    tdd_gate._claim_path(repo, "TASK-001", "default").unlink()

    assert _red(repo) == 0  # recovery: 0 без прогона селектора
    assert tdd_gate.load_claim(repo, "TASK-001", "default") is not None


def test_abandon_frees_the_task_for_an_honest_new_red(repo: Path) -> None:
    """После `abandon` recovery молчит и `red` начинает цикл заново."""
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0
    first_claim = tdd_gate.load_claim(repo, "TASK-001", "default")
    assert first_claim is not None
    first_red = first_claim.red_sha

    assert tdd_gate.cmd_abandon(repo, "assertion противоречит контракту TASK-009") == 0

    assert tdd_gate.load_claim(repo, "TASK-001", "default") is None, "claim снят"
    record = tdd_gate.load_abandon(repo, "TASK-001", "default")
    assert record is not None
    assert record["red_shas"] == [first_red]
    assert "TASK-009" in str(record["reason"]), "причина сохранена дословно"
    assert tdd_gate.find_red_commit_by_trailer(repo, "TASK-001", "default") is None, (
        "recovery больше не подхватывает отвергнутый чекпоинт"
    )


def test_abandon_keeps_the_red_commit_in_history(repo: Path) -> None:
    """Отказ ничего не разрушает: коммит на месте, история не переписана.

    Ровно этим `abandon` отличается от `git reset --hard`, которым дыру
    приходилось лечить дважды за фазу w-runtime.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0
    claim = tdd_gate.load_claim(repo, "TASK-001", "default")
    assert claim is not None
    red_sha = claim.red_sha

    assert tdd_gate.cmd_abandon(repo, "негодный assertion") == 0

    assert tdd_gate._commit_exists(repo, red_sha)
    assert tdd_gate._is_ancestor(repo, red_sha, tdd_gate.head_sha(repo))


def test_abandon_is_idempotent(repo: Path) -> None:
    """Повтор не создаёт второй коммит: отказ уже зафиксирован."""
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0
    assert tdd_gate.cmd_abandon(repo, "причина") == 0
    head_after_first = tdd_gate.head_sha(repo)

    assert tdd_gate.cmd_abandon(repo, "причина") == 0

    assert tdd_gate.head_sha(repo) == head_after_first


def test_a_second_abandon_does_not_revive_the_first_rejected_red(repo: Path) -> None:
    """Copilot, PR #15: отказы накапливаются, а не затирают друг друга.

    Цикл «abandon → новый red → abandon» законен: второй тест тоже может
    оказаться негодным. Если запись хранит только последний `red_sha`, recovery
    находит предыдущий — уже отвергнутый — чекпоинт и восстанавливает claim из
    него. Круг disputatio#12 замыкается заново, причём тише прежнего: запись об
    отказе на диске есть, и выглядит она исправной.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0
    first = tdd_gate.load_claim(repo, "TASK-001", "default")
    assert first is not None
    assert tdd_gate.cmd_abandon(repo, "первый assertion негоден") == 0

    # Второй честный red — другой файл, поэтому и другой red-коммит.
    _write_failing(repo, OTHER_PATH, "test_y")
    assert _red(repo, OTHER_SELECTOR) == 0
    second = tdd_gate.load_claim(repo, "TASK-001", "default")
    assert second is not None and second.red_sha != first.red_sha
    assert tdd_gate.cmd_abandon(repo, "и второй тоже") == 0

    assert tdd_gate.abandoned_red_shas(repo, "TASK-001", "default") == (
        first.red_sha,
        second.red_sha,
    ), "оба отказа обязаны остаться в силе"
    assert tdd_gate.find_red_commit_by_trailer(repo, "TASK-001", "default") is None, (
        "recovery не должен воскрешать первый отвергнутый чекпоинт"
    )


def test_abandon_keeps_the_reason_of_every_rejection(repo: Path) -> None:
    """Причина каждого отказа сохраняется, а не только последнего.

    Верхнеуровневый `reason` описывает свежий отказ; без истории мотив
    предыдущих терялся бы, а именно он объясняет, почему задача переигрывалась
    дважды.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0
    assert tdd_gate.cmd_abandon(repo, "причина номер один") == 0
    _write_failing(repo, OTHER_PATH, "test_y")
    assert _red(repo, OTHER_SELECTOR) == 0
    assert tdd_gate.cmd_abandon(repo, "причина номер два") == 0

    record = tdd_gate.load_abandon(repo, "TASK-001", "default")
    assert record is not None
    history = record["history"]
    assert isinstance(history, list) and len(history) == 2
    assert [entry["reason"] for entry in history] == [
        "причина номер один",
        "причина номер два",
    ]


def test_abandon_refuses_a_claim_closed_by_a_verdict(repo: Path) -> None:
    """Отказ от закрытого PASS — supersession, в v1 запрещена.

    Иначе зелёный вердикт можно было бы стереть постфактум, и evidence
    перестала бы что-либо доказывать.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0
    _implement(repo)
    assert tdd_gate.cmd_verify(repo) == 0
    verdict = tdd_gate.load_verdict(repo, "TASK-001", "default")
    assert verdict is not None and verdict.verdict == "PASS"

    assert tdd_gate.cmd_abandon(repo, "хочу переиграть") == 3


def test_abandon_requires_a_reason(repo: Path) -> None:
    """Запись без причины бесполезна для того, кто читает её через месяц."""
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0

    assert tdd_gate.cmd_abandon(repo, "   ") == 3


# --- disputatio#13: замок распространяется на все заклеймленные файлы -------


def _two_claims(repo: Path) -> None:
    """Namespace с двумя claim'ами: TASK-001 закрыт PASS, TASK-002 pending.

    Второй claim нужен, чтобы у `verify` была «текущая» задача, отличная от
    той, чей замок проверяется кросс-проверкой: иначе оба пути (строгий для
    своего файла и кросс-проверка для чужих) слились бы в один.
    """
    write_tasks(repo, "tasks.md", ONE_RUNNING)
    _write_failing(repo)
    assert _red(repo) == 0
    _implement(repo)
    assert tdd_gate.cmd_verify(repo) == 0
    _runner_auto_commit(repo)

    write_tasks(repo, "tasks.md", TWO_TASKS_SECOND_RUNNING)
    _write_failing(repo, OTHER_PATH, "test_y")
    assert _red(repo, OTHER_SELECTOR) == 0
    _implement(repo, "test_y")


def test_verify_catches_tampering_with_another_tasks_locked_test(repo: Path) -> None:
    """Правка ЧУЖОГО залоченного теста ловится прибором, а не конституцией."""
    _two_claims(repo)
    (repo / TEST_PATH).write_text(
        "def test_x():\n    assert True  # выхолощен\n", encoding="utf-8"
    )

    code = tdd_gate.cmd_verify(repo)

    assert code == 3, "чужой залоченный файл разошёлся — verify обязан отказать"


def test_locked_drift_sees_uncommitted_tampering(repo: Path) -> None:
    """Подмена в рабочем дереве, ещё не закоммиченная, — тоже подмена."""
    _two_claims(repo)
    (repo / TEST_PATH).write_text("def test_x():\n    pass\n", encoding="utf-8")

    drift = tdd_gate.locked_test_drift(repo, "default")

    assert any(TEST_PATH in item for item in drift)


def test_a_deleted_locked_test_is_drift_too(repo: Path) -> None:
    """Удаление залоченного теста — предельный случай его изменения."""
    _two_claims(repo)
    (repo / TEST_PATH).unlink()

    drift = tdd_gate.locked_test_drift(repo, "default")

    assert any("удалён" in item for item in drift)


def test_intact_locks_produce_no_drift(repo: Path) -> None:
    """Контроль: без правок кросс-проверка молчит и verify проходит."""
    _two_claims(repo)

    assert tdd_gate.locked_test_drift(repo, "default") == []
    assert tdd_gate.cmd_verify(repo) == 0


def _annotate(repo: Path, path: str, name: str, extra: str = "") -> None:
    """Семантически нейтральная правка залоченного теста: аннотация типа.

    Тест остаётся зелёным и проверяет то же самое — меняются только байты.
    Ровно этот случай оператор санкционировал вживую на приёмке w-runtime.
    """
    (repo / path).write_text(
        "from pathlib import Path\n\n\n"
        f"def {name}() -> None:\n"
        f'    assert Path("src/mod.py").read_text().strip() == "READY-{name}"\n'
        f"{extra}",
        encoding="utf-8",
    )


# --- disputatio#13: repair как законный выход -------------------------------


def test_repair_makes_a_sanctioned_edit_legal(repo: Path) -> None:
    """Санкционированная правка перестаёт быть находкой — и только она."""
    _two_claims(repo)
    _annotate(repo, TEST_PATH, "test_x")
    assert tdd_gate.locked_test_drift(repo, "default") != []

    assert tdd_gate.cmd_repair(repo, TEST_PATH, "аннотация типа, семантика та же") == 0

    assert tdd_gate.locked_test_drift(repo, "default") == []


def test_repair_records_provenance(repo: Path) -> None:
    """Запись ремонта хранит путь, владельца, принятый blob и причину.

    Объяснение в теле коммита прибор не читает; запись — читает.
    """
    _two_claims(repo)
    _annotate(repo, TEST_PATH, "test_x")
    assert tdd_gate.cmd_repair(repo, TEST_PATH, "почему именно так") == 0

    accepted = tdd_gate.load_repairs(repo, "default")

    assert TEST_PATH in accepted
    assert accepted[TEST_PATH] == [tdd_gate._blob_now(repo, TEST_PATH)]


def test_repair_of_a_further_edit_keeps_the_earlier_blob_legal(repo: Path) -> None:
    """Второй ремонт не объявляет первый нарушением: оба blob'а приняты."""
    _two_claims(repo)
    _annotate(repo, TEST_PATH, "test_x")
    assert tdd_gate.cmd_repair(repo, TEST_PATH, "первая правка") == 0
    first = tdd_gate._blob_now(repo, TEST_PATH)
    _annotate(repo, TEST_PATH, "test_x", extra="  # и ещё\n")
    assert tdd_gate.cmd_repair(repo, TEST_PATH, "вторая правка") == 0

    accepted = tdd_gate.load_repairs(repo, "default")[TEST_PATH]

    assert first in accepted
    assert tdd_gate._blob_now(repo, TEST_PATH) in accepted


def test_repair_records_of_slug_colliding_paths_do_not_overwrite(
    repo: Path,
) -> None:
    """Copilot, PR #15: два пути с одинаковым slug не делят одну запись.

    `tests/a-b.py` и `tests/a/b.py` при наивном slug дают одно имя файла, и
    запись второго затирала бы первую: принятые blob'ы первого исчезали бы, а
    его законная правка снова становилась находкой. Для operator-evidence это
    потеря provenance, поэтому имя записи обязано быть инъективным.
    """
    flat, nested = "tests/a-b.py", "tests/a/b.py"
    assert tdd_gate._repair_slug(flat) != tdd_gate._repair_slug(nested), (
        "slug обязан различать пути, схлопывающиеся при замене разделителей"
    )

    write_tasks(repo, "tasks.md", ONE_RUNNING)
    (repo / "tests" / "a").mkdir()
    for path, name in ((flat, "test_flat"), (nested, "test_nested")):
        _write_failing(repo, path, name)
    # Один claim владеет flat, другой — nested; ремонт объявляется для обоих.
    assert _red(repo, f"{flat}::test_flat") == 0
    _implement(repo, "test_flat")
    assert tdd_gate.cmd_verify(repo) == 0
    _runner_auto_commit(repo)
    write_tasks(repo, "tasks.md", TWO_TASKS_SECOND_RUNNING)
    assert _red(repo, f"{nested}::test_nested") == 0
    _implement(repo, "test_nested")

    _annotate(repo, flat, "test_flat")
    assert tdd_gate.cmd_repair(repo, flat, "аннотация в flat") == 0
    _annotate(repo, nested, "test_nested")
    assert tdd_gate.cmd_repair(repo, nested, "аннотация в nested") == 0

    accepted = tdd_gate.load_repairs(repo, "default")
    assert tdd_gate._blob_now(repo, flat) in accepted.get(flat, []), (
        "запись первого файла обязана уцелеть после ремонта второго"
    )
    assert tdd_gate._blob_now(repo, nested) in accepted.get(nested, [])
    assert tdd_gate.locked_test_drift(repo, "default") == []


def test_repair_refuses_a_file_no_claim_owns(repo: Path) -> None:
    """Ремонт объявляется только для залоченного файла.

    Иначе командой можно было бы «санкционировать» что угодно под видом
    операторского действия, и запись потеряла бы смысл.
    """
    _two_claims(repo)
    (repo / "tests" / "test_free.py").write_text(
        "def test_free():\n    assert True\n", encoding="utf-8"
    )

    assert tdd_gate.cmd_repair(repo, "tests/test_free.py", "просто так") == 3


def test_repair_refuses_paths_outside_tests(repo: Path) -> None:
    """Продуктовый код ремонтируется обычным коммитом, а не этой командой."""
    _two_claims(repo)

    assert tdd_gate.cmd_repair(repo, "src/mod.py", "не сюда") == 3


# --- disputatio#8: remedy-подсказка называет действующий выход --------------


def test_the_remedy_message_points_at_abandon_and_repair(repo: Path) -> None:
    """Подсказка обязана называть работающее действие, а не удаление claim'а.

    Прежний текст советовал «оператор удаляет claim и verdict», что не
    работает: recovery восстановит claim из red-коммита.
    """
    message = tdd_gate._test_path_changed_remedy("TASK-001", TEST_PATH, "default")

    assert "abandon" in message
    assert "repair" in message
    assert "Удаление claim'а руками remedy НЕ является" in message
