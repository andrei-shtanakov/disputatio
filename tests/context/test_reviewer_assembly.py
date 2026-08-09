"""Сборка промпта ревьюера как целого: TASK-005, [DESIGN-006].

`test_reviewer.py` пинит содержимое секций — какие пути, гейты и замечания
попадают в промпт. Мутационная проба показала, что мимо него проходит ровно
то, что содержимым секций не описывается: четыре мутанта реализации пережили
весь `tests/context/`.

* пути `proposal`/`patch` можно поменять местами — оба остаются в промпте,
  и ни один ассерт не замечает, что ревьюеру велено читать не тот файл;
* номер раунда можно выкинуть из вводного абзаца — «7» всё равно найдётся
  в промпте, потому что в `diff_stats` попадаются те же цифры;
* заголовок секции материалов можно склеить со строкой пути — заголовок
  как таковой не проверялся нигде;
* сверку раунда можно сделать односторонней (`artifact.round < expected`
  вместо `!=`) — все проверки раунда в `test_reviewer.py` подставляют
  артефакт из раунда РАНЬШЕ ожидаемого, поэтому вторая половина guard'а
  ничем не доказана.

Файл отдельный, а не дополнение к `test_reviewer.py`: тот байт-заморожен
red-чекпоинтом TASK-005 и правке не подлежит.

Пути здесь нарочно не содержат слов «proposal» и «patch»: иначе проверка
пары «метка ↔ путь» проходила бы сама по себе, на подстроке из имени файла.
"""

import importlib
from types import ModuleType

import pytest

from disputatio.contracts.base import Role
from disputatio.contracts.decision import Decision, Outcome
from disputatio.contracts.review import Review, Verdict
from disputatio.contracts.session import Mode, TaskSpec
from disputatio.contracts.verification import (
    DiffStats,
    OverallStatus,
    VerificationReport,
)

PROPOSAL_PATH = ".disputatio/rounds/009/PLAN.md"
PATCH_PATH = ".disputatio/rounds/009/DELTA.bin"


def _module(name: str) -> ModuleType:
    """Импортирует модуль пакета `context`; отсутствие — assertion."""
    try:
        return importlib.import_module(f"disputatio.context.{name}")
    except ImportError as exc:  # pragma: no cover - только на red-чекпоинте
        raise AssertionError(
            f"disputatio.context.{name} не импортируется: {exc}"
        ) from exc


def _verification(round_no: int) -> VerificationReport:
    """Отчёт проверок раунда `round_no` без гейтов — минимум для промпта."""
    return VerificationReport(
        round=round_no,
        gates=[],
        overall=OverallStatus.PASS,
        diff_stats=DiffStats(files=1, insertions=2, deletions=3),
    )


def _review(round_no: int) -> Review:
    """`review.json` раунда `round_no`; содержимое здесь не важно — важен раунд."""
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.5,
        issues=[],
        checked=["src/x.py"],
        summary="нужны правки",
    )


def _decision(round_no: int) -> Decision:
    """`decision.json` раунда `round_no`; содержимое здесь не важно — важен раунд."""
    return Decision(
        round=round_no,
        outcome=Outcome.CONTINUE,
        reason="continue_revise_cycle",
        open_issues_carried=[],
        next_round_directive=None,
    )


def _prompt(round_no: int = 9) -> str:
    """Минимальный промпт раунда `round_no`; цифры `diff_stats` — не раунд."""
    reviewer = _module("reviewer")
    return reviewer.build_reviewer_prompt(
        task=TaskSpec(prompt="Почини ретраи клиента.", mode=Mode.DEVELOP),
        round=round_no,
        proposal_path=PROPOSAL_PATH,
        patch_path=PATCH_PATH,
        verification=_verification(round_no),
    )


def _line_with(prompt: str, needle: str) -> str:
    """Строка промпта, содержащая `needle`; ровно одна — иначе assertion."""
    lines = [line for line in prompt.splitlines() if needle in line]
    assert len(lines) == 1, f"{needle!r} встречается в {len(lines)} строках"
    return lines[0]


def test_each_material_path_is_labelled_with_its_own_kind() -> None:
    """Перепутанные местами пути отправили бы ревьюера читать не тот файл."""
    prompt = _prompt()

    proposal_line = _line_with(prompt, PROPOSAL_PATH)
    patch_line = _line_with(prompt, PATCH_PATH)

    assert "proposal" in proposal_line
    assert "patch" not in proposal_line
    assert "patch" in patch_line
    assert "proposal" not in patch_line


def test_intro_names_the_current_round() -> None:
    """Ревьюер обязан знать раунд: `review.json` пишется именно за него (§4.4).

    Номер ищется во вводном абзаце — до первой секции: в самом отчёте
    проверок цифры встречаются и без всякого раунда.
    """
    sections = _module("sections")
    prompt = _prompt(round_no=9)

    intro = prompt[: prompt.index(sections.TASK_TITLE)]
    assert "9" in intro


def test_materials_title_opens_its_own_section() -> None:
    """Заголовок материалов — отдельная строка после пустой, а не приклеен к пути."""
    reviewer = _module("reviewer")
    prompt = _prompt()

    assert f"\n\n{reviewer.MATERIALS_TITLE}\n" in prompt
    # Заголовок и первый путь — соседние строки одной секции, без разрыва.
    materials = prompt[prompt.index(reviewer.MATERIALS_TITLE) :]
    assert "\n\n" not in materials[: materials.index(PROPOSAL_PATH)]


@pytest.mark.parametrize(
    ("param", "expected_round"),
    [("verification", 3), ("prior_review", 2), ("prior_decision", 2)],
)
def test_artifact_from_a_later_round_is_rejected(
    param: str, expected_round: int
) -> None:
    """Сверка раунда двусторонняя: артефакт из БУДУЩЕГО раунда — тоже `ValueError`.

    `test_reviewer.py` подставляет во все три параметра артефакт раунда
    более РАННЕГО, чем ожидаемый, поэтому `artifact.round < expected` вместо
    `!=` переживает весь `tests/context/`. Между тем именно эта половина
    guard'а ловит самый вероятный сбой оркестратора: resume, при котором в
    промпт раунда N поехал `verification.json` уже начатого раунда N+1 —
    ревьюер вынес бы вердикт по гейтам чужого кода и не узнал бы об этом.
    """
    reviewer = _module("reviewer")
    later = {
        "verification": _verification(expected_round + 1),
        "prior_review": _review(expected_round + 1),
        "prior_decision": _decision(expected_round + 1),
    }

    with pytest.raises(ValueError) as excinfo:
        reviewer.build_reviewer_prompt(
            task=TaskSpec(prompt="Почини ретраи клиента.", mode=Mode.DEVELOP),
            round=3,
            proposal_path=PROPOSAL_PATH,
            patch_path=PATCH_PATH,
            **{"verification": _verification(3), param: later[param]},
        )

    message = str(excinfo.value)
    # Только диагностическая часть: в постоянном хвосте есть «§6.2», и на
    # нём ассерт про ожидавшийся раунд 2 прошёл бы сам собой.
    diagnostic = message.split(";")[0]
    assert param in diagnostic
    assert str(expected_round) in diagnostic
