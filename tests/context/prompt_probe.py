"""Эталонные промпты для сравнения байтов между процессами (TASK-007).

Инструмент проверки, а не проверка: префикса `test_` нет, поэтому pytest
модуль не собирает. Он запускается двумя способами — импортом из
`test_prompt_determinism.py` и как отдельный скрипт (`python prompt_probe.py`)
в подпроцессе с другим `PYTHONHASHSEED`. Отсюда два ограничения: никаких
относительных импортов и никакой зависимости от pytest.

Вход подобран так, чтобы недетерминированный обход коллекции был ВИДЕН:
шесть замечаний одной severity (сортировка §6.1 их не разводит, порядок
задаётся только обходом входа), половина открыта, половина заявлена
решённой — непустыми оказываются обе секции замечаний, и авторская, и
ревьюерская.

Модуль живёт под `tests/`, но I/O не делает: `main()` печатает дайджесты в
stdout, читать ему нечего.
"""

import hashlib

from disputatio.context import author, reviewer
from disputatio.contracts.base import Role
from disputatio.contracts.decision import Decision, Outcome
from disputatio.contracts.review import Issue, Review, Severity, Verdict
from disputatio.contracts.session import Mode, TaskSpec
from disputatio.contracts.verification import (
    DiffStats,
    GateResult,
    GateStatus,
    OverallStatus,
    VerificationReport,
)

PROPOSAL_PATH = ".disputatio/rounds/002/proposal.md"
PATCH_PATH = ".disputatio/rounds/002/changes.patch"

#: Замечания прошлого раунда. Severity у всех одна — иначе порядок задавала
#: бы сортировка, и обход `open_issues_carried` перестал бы быть виден.
ISSUE_IDS = ("R1-1", "R1-2", "R1-3", "R1-4", "R1-5", "R1-6")
#: Строгое непустое подмножество: открытые уезжают автору, остальные —
#: ревьюеру как «заявлено решённым».
CARRIED = ISSUE_IDS[:3]

#: Тексты вокруг замечаний не называют их id: иначе проверка «id из CARRIED
#: нет в промпте ревьюера» ловила бы вхождение из директивы, а не из секции.
_DIRECTIVE = "Закройте блокирующие замечания прошлого раунда."
_TAIL = "E   assert 1 == 2\n1 failed, 3 passed"


def build_prompts() -> tuple[str, str]:
    """Промпты автора и ревьюера раунда 2 из одних и тех же артефактов."""
    review = _review(1)
    decision = _decision(1)
    author_prompt = author.build_author_prompt(
        task=_task(),
        round=2,
        prior_review=review,
        prior_verification=_verification(1),
        prior_decision=decision,
    )
    reviewer_prompt = reviewer.build_reviewer_prompt(
        task=_task(),
        round=2,
        proposal_path=PROPOSAL_PATH,
        patch_path=PATCH_PATH,
        verification=_verification(2),
        prior_review=review,
        prior_decision=decision,
    )
    return author_prompt, reviewer_prompt


def digests() -> list[str]:
    """sha256 каждого промпта — то, что подпроцесс отдаёт в stdout."""
    return [hashlib.sha256(prompt.encode()).hexdigest() for prompt in build_prompts()]


def main() -> None:
    """Печатает дайджесты построчно; их и сравнивает вызывающий тест."""
    print("\n".join(digests()))


def _task() -> TaskSpec:
    """Задача пользователя — единственная обязательная секция обоих промптов."""
    return TaskSpec(prompt="Почините клиент HTTP.", mode=Mode.DEVELOP)


def _review(round_no: int) -> Review:
    """`review.json` раунда `round_no` с шестью равными по severity замечаниями."""
    issues = [
        Issue(
            id=issue_id,
            severity=Severity.BLOCKER,
            file="src/client.py",
            claim=f"замечание номер {position}",
            evidence=f"src/client.py:{position}",
        )
        for position, issue_id in enumerate(ISSUE_IDS, start=1)
    ]
    return Review(
        round=round_no,
        role=Role.REVIEWER,
        verdict=Verdict.REQUEST_CHANGES,
        confidence=0.4,
        issues=issues,
        checked=["src/client.py"],
        summary="нужны правки",
    )


def _decision(round_no: int) -> Decision:
    """`decision.json` раунда `round_no`: что осталось открытым и директива."""
    return Decision(
        round=round_no,
        outcome=Outcome.CONTINUE,
        reason="continue_revise_cycle",
        open_issues_carried=list(CARRIED),
        next_round_directive=_DIRECTIVE,
    )


def _verification(round_no: int) -> VerificationReport:
    """Отчёт раунда `round_no`: один проваленный гейт и один прошедший."""
    gates = [
        GateResult(
            name="pytest",
            cmd="uv run pytest -q",
            status=GateStatus.FAIL,
            exit_code=1,
            tail=_TAIL,
        ),
        GateResult(
            name="ruff",
            cmd="ruff check .",
            status=GateStatus.PASS,
            exit_code=0,
            tail="All checks passed!",
        ),
    ]
    return VerificationReport(
        round=round_no,
        gates=gates,
        overall=OverallStatus.FAIL,
        diff_stats=DiffStats(files=1, insertions=2, deletions=3),
    )


if __name__ == "__main__":
    main()
