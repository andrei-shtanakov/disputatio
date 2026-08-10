"""Шаг `EXPORTING`: `result/*` + `manifest.json` ([DESIGN-017], [DESIGN-018]).

Экспорт — последний шаг сессии и самый простой: он ничего не решает и почти
ничего не пишет. Всё, что он делает, — называет раунд-источник, переносит
его артефакты в `result/` и записывает манифест о том, чем сессия кончилась.
Четыре обязательства, и все четыре про то, откуда берётся ответ:

1. **Раунд-источник назван состоянием, а не найден на диске.** Источник —
   `state.current_round` на входе в терминальную фазу: терминальная цепочка
   §5 счётчик раунда не двигает, поэтому в `EXPORTING` он всё ещё равен
   раунду, который вынес решение. «Последний раунд в `rounds/`» совпадал бы
   с ним ровно до первого обрыва посреди `PROPOSING` — и разошёлся бы молча,
   уже в манифесте. Тот же номер уходит в `source_round`, чтобы потребитель
   не гадал, из какого раунда собран результат.
2. **`converged` вычисляет ядро.** Ровно один вызов `core.is_partial` —
   собственной таблицы частичных исходов здесь нет и быть не должно:
   разъехавшись с §5, она объявила бы сошедшейся сессию, которую ядро
   эскалировало пользователю. Ветка без сходимости отличается от сходимости
   только СОДЕРЖИМЫМ манифеста ([DESIGN-018]), поэтому отдельного кодового
   пути у неё нет.
3. **`files` считает писатель.** Ключ `files` с sha256 дописывает
   `events.write_result` по тем байтам, что реально легли на диск, и
   перекрывает одноимённое поле вызывающей стороны. Runtime его не
   подставляет: checksum, посчитанный до записи, описывал бы намерение, а не
   результат. Оттуда же берётся и «манифест пишется последним» — весь
   экспорт уходит в ОДИН вызов, и другого писателя у шага нет.
4. **`open_issues` — пересечение двух артефактов раунда** ([DESIGN-018]):
   замечания его ревью, отфильтрованные `open_issues_carried` его решения.
   Считает пересечение `history.carried_issues` — тот же читатель, которым
   собирается снимок `DECIDING`, и второй его копией здесь манифест
   рассказывал бы об открытых замечаниях не то, что читало ядро. Записи
   уходят полными (§3.2): человек, читающий манифест, по одному `id` не
   узнает, что осталось несделанным.

Переход в `DONE` — после записи: фаза, за которой не стоит `result/`,
объявила бы законченной сессию без артефакта, а resume переигрывать её уже
не стал бы. `DONE`, а не `FAILED`, и для эскалации тоже ([REQ-018]):
остановка по деадлоку или бюджету — штатный исход протокола, и терминал у
неё общий с сошедшейся сессией, как общ и сам этот шаг.
"""

from pathlib import Path
from typing import Any, Final

from disputatio.contracts import (
    SCHEMA_V1,
    Decision,
    Issue,
    SessionPhase,
    SessionState,
)
from disputatio.core import is_partial
from disputatio.events import write_result
from disputatio.runtime.history import carried_issues, load_decision, load_patch
from disputatio.runtime.layout import PROPOSAL_NAME, round_artifact
from disputatio.runtime.steps import StepContext

RESULT_MD_NAME: Final = "result.md"
"""Имя экспортированного предложения: `proposal.md` раунда-источника."""

RESULT_PATCH_NAME: Final = "result.patch"
"""Имя экспортированного патча: `changes.patch` того же раунда."""


def export(ctx: StepContext) -> None:
    """Шаг EXPORTING: `result/*` + `manifest.json`, затем переход в `DONE`.

    Патч подставляется пустой строкой, если раунд его не оставил
    ([REQ-013]): `result.patch` обязан существовать в любом случае — «автор
    ничего не менял» и «экспорт до патча не дошёл» это разные факты, и
    различает их только наличие файла со своим checksum.
    """
    root = ctx.root
    round_no = ctx.round

    decision = _source_decision(root, round_no)
    write_result(
        root,
        {
            RESULT_MD_NAME: _source_proposal(root, round_no),
            RESULT_PATCH_NAME: load_patch(root, round_no) or "",
        },
        _manifest(ctx.fsm.state, decision, carried_issues(root, round_no), round_no),
    )

    ctx.fsm.transition(SessionPhase.DONE)


def _manifest(
    state: SessionState,
    decision: Decision,
    open_issues: tuple[Issue, ...],
    round_no: int,
) -> dict[str, Any]:
    """Манифест §3.2 без ключа `files` — его дописывает `write_result`.

    Исход и причина остановки берутся у решения раунда-источника, а не
    выводятся из фазы: `stop_reason` — тот самый machine-readable код,
    который §5 записал в `decision.json`, и вторая его формулировка здесь
    разошлась бы с историей сессии.

    `rounds_total` равен `source_round` не по совпадению: счётчик раунда
    растёт только на `IDLE → PROPOSING` и на revise-петле, поэтому номер
    последнего раунда и есть их количество. Два ключа сохранены оба —
    потребитель манифеста не обязан знать это правило.

    `budget_used` сведён к двум счётчикам §3.2: `cost_usd_est` — оценка
    LiteLLM-прокси, которой в v1 нет, и печатать её нулём значило бы
    утверждать, что сессия ничего не стоила.

    `open_issues` печатается всегда, в том числе пустым списком: ключ,
    появляющийся только у частичного исхода, сделал бы «замечаний не
    осталось» и «манифест собран другой веткой» неразличимыми — а вся
    разница двух исходов по [DESIGN-018] и есть содержимое этого документа.
    """
    return {
        "schema": SCHEMA_V1,
        "session": state.session_id,
        "converged": not is_partial(decision.outcome),
        "outcome": decision.outcome.value,
        "stop_reason": decision.reason,
        "source_round": round_no,
        "rounds_total": round_no,
        "budget_used": {
            "tokens": state.budget_used.tokens,
            "wall_seconds": state.budget_used.wall_seconds,
        },
        "open_issues": [_issue_entry(issue) for issue in open_issues],
    }


def _issue_entry(issue: Issue) -> dict[str, str]:
    """Открытое замечание в манифесте — четыре поля §3.2, не весь артефакт.

    Сериализовать `Issue` целиком было бы короче, но манифест — документ для
    человека, а не копия `review.json`: `evidence` и `suggestion` относятся к
    разбору раунда и живут в его артефактах, где их не перепутать с тем, что
    сессия оставила несделанным.
    """
    return {
        "id": issue.id,
        "severity": issue.severity.value,
        "file": issue.file,
        "claim": issue.claim,
    }


def _source_decision(root: Path, round_no: int) -> Decision:
    """Решение раунда-источника; его отсутствие — ошибка порядка.

    `AssertionError`, а не доменная ошибка: в `EXPORTING` сессию заводит
    только `apply_decision`, то есть `decision.json` к этому моменту уже
    лежит на диске. Пустое место здесь означает сломанную диспетчеризацию
    цикла, а не действие пользователя, — а манифест без исхода объявил бы
    результат экспортированным, не сказав, чем сессия кончилась.
    """
    decision = load_decision(root, round_no)
    if decision is None:
        raise AssertionError(
            f"нет decision.json раунда {round_no:03d}: шаг EXPORTING вызван "
            "до DECIDING — сказать в манифесте, чем кончилась сессия, не из "
            "чего"
        )
    return decision


def _source_proposal(root: Path, round_no: int) -> str:
    """`proposal.md` раунда-источника; его отсутствие — ошибка порядка.

    `AssertionError` по той же причине: до `EXPORTING` раунд прошёл
    `PROPOSING`, а тот пишет предложение до любого перехода. Пустой
    `result.md` с честным sha256 выглядел бы законным экспортом сессии, в
    которой автор ничего не написал.
    """
    path = round_artifact(root, round_no, PROPOSAL_NAME)
    if not path.is_file():
        raise AssertionError(
            f"нет proposal.md раунда {round_no:03d}: шаг EXPORTING вызван до "
            "PROPOSING — экспортировать в result.md нечего"
        )
    return path.read_text(encoding="utf-8")
