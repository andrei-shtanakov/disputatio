"""Mutation-freedom рабочего дерева (TASK-009, [REQ-009], [DESIGN-010]).

Инвариант INV-10: снимок `git status --porcelain` до и после прогона
типового набора gates идентичен. Обеспечивает его состав операций пакета
([DESIGN-010]) — раннер сам ничего не пишет в дерево и не выполняет
мутирующих git-подкоманд; за поведение самих gates отвечает конфигурация,
поэтому «мутирующий gate» здесь — негативный контроль снимка, а не
нарушение контракта Verifier'а.

Снимок дерева и драйвер поверхности Verifier'а живут в `.mutation_probe` —
не-тестовом модуле рядом. Импорт обёрнут: на момент red-чекпоинта модуля
ещё нет, и импорт на уровне теста сломал бы collection всего каталога.
Хелпер пересобирает `ImportError` в `AssertionError` — гейт принимает red
только при падении assertion'ом.

Ограничение: `VerifierRunner.verify()` ([DESIGN-003]) ещё не написан
(TASK-008), поэтому прогоняется его сегодняшняя поверхность — `run_gate`
по каждому `GateSpec` плюс `collect_diff_stats`. Это ровно те операции,
из которых `verify()` и будет собран; после TASK-008 меняется драйвер в
`.mutation_probe`, а не эти тесты.
"""

import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from disputatio.contracts.verification import DiffStats, GateStatus

if TYPE_CHECKING:
    from disputatio.verifier import GateSpec

_WRITE_IGNORED = (
    "import pathlib; "
    "d = pathlib.Path('.pytest_cache/v/cache'); "
    "d.mkdir(parents=True, exist_ok=True); "
    "(d / 'lastfailed').write_text('{}')"
)
_WRITE_UNTRACKED = "import pathlib; pathlib.Path('leftover.txt').write_text('junk')"
_MUTATE_TRACKED = "import pathlib; pathlib.Path('tracked.txt').write_text('mutated\\n')"


def _probe() -> ModuleType:
    """Импортирует `.mutation_probe`; отсутствие модуля — `AssertionError`."""
    try:
        from . import mutation_probe
    except ImportError as exc:  # red-фаза: mutation_probe.py ещё не создан
        raise AssertionError("tests/verifier/mutation_probe.py ещё не создан") from exc
    return mutation_probe


def _python_cmd(code: str) -> str:
    """Собирает команду `<интерпретатор> -c <code>` с корректным quoting'ом."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_typical_gate_set_leaves_porcelain_unchanged(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """[REQ-009]: типовой набор gates не меняет снимок рабочего дерева."""
    probe = _probe()
    specs = [
        make_gate_spec(name="tests", cmd=_python_cmd("raise SystemExit(0)")),
        make_gate_spec(name="lint", cmd=_python_cmd("raise SystemExit(1)")),
        make_gate_spec(name="types", cmd="disputatio-never-run --check", enabled=False),
        make_gate_spec(name="missing", cmd="disputatio-no-such-binary --version"),
        make_gate_spec(name="cache", cmd=_python_cmd(_WRITE_IGNORED)),
    ]

    before = probe.porcelain(tmp_git_repo)
    assert before == ""

    run = probe.run_verifier_surface(specs, tmp_git_repo)

    # Не-вакуумность: набор действительно прогнан и покрыл pass/fail/skip —
    # инвариант проверяется на работавших gates, а не на пустом множестве.
    assert [gate.status for gate in run.gates] == [
        GateStatus.PASS,
        GateStatus.FAIL,
        GateStatus.SKIP,
        GateStatus.SKIP,
        GateStatus.PASS,
    ]
    assert (tmp_git_repo / ".pytest_cache" / "v" / "cache" / "lastfailed").exists()
    assert run.diff_stats == DiffStats(files=0, insertions=0, deletions=0)

    assert probe.porcelain(tmp_git_repo) == before


def test_tracked_file_mutation_is_detected(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """Негативный контроль: снимок обязан заметить порчу tracked-файла.

    Без этого теста инвариант из `test_typical_gate_set_leaves_porcelain_
    unchanged` доказывал бы лишь то, что снимок слеп: константа проходит
    сравнение «до == после» ничуть не хуже честного `git status`.
    """
    probe = _probe()
    rogue = make_gate_spec(name="rogue", cmd=_python_cmd(_MUTATE_TRACKED))

    before = probe.porcelain(tmp_git_repo)
    run = probe.run_verifier_surface([rogue], tmp_git_repo)
    after = probe.porcelain(tmp_git_repo)

    assert [gate.status for gate in run.gates] == [GateStatus.PASS]
    assert (tmp_git_repo / "tracked.txt").read_text(encoding="utf-8") == "mutated\n"

    assert after != before
    assert "tracked.txt" in after
    assert run.diff_stats == DiffStats(files=1, insertions=1, deletions=1)


def test_write_into_ignored_dir_is_not_a_mutation(
    tmp_git_repo: Path, make_gate_spec: "Callable[..., GateSpec]"
) -> None:
    """[REQ-009]: запись в `.gitignore`'d каталог мутацией не считается."""
    probe = _probe()
    cache = make_gate_spec(name="cache", cmd=_python_cmd(_WRITE_IGNORED))

    before = probe.porcelain(tmp_git_repo)
    run = probe.run_verifier_surface([cache], tmp_git_repo)

    assert [gate.status for gate in run.gates] == [GateStatus.PASS]
    assert (tmp_git_repo / ".pytest_cache" / "v" / "cache" / "lastfailed").exists()
    assert probe.porcelain(tmp_git_repo) == before

    # Не-вакуумность: невидимость даёт `.gitignore`, а не слепота снимка —
    # такой же по природе новый файл вне игнорируемого каталога снимок видит.
    leftover = make_gate_spec(name="leftover", cmd=_python_cmd(_WRITE_UNTRACKED))
    probe.run_verifier_surface([leftover], tmp_git_repo)

    after = probe.porcelain(tmp_git_repo)
    assert after != before
    assert "leftover.txt" in after
