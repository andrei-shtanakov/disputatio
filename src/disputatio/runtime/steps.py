"""Шаги оркестраторного цикла ([DESIGN-003]…[DESIGN-007]).

Шаг — это порядок вызовов и I/O, и ничего больше: стоп-условия §5 живут в
`core.decide`, граф §2 — в `core.SessionFsm`, валидация §4.4 — в
`contracts.validate_review`, раскладка секций §6 — в `context`. Всё, что
здесь можно испортить, портится перестановкой строк, а не логикой, — поэтому
порядок операций каждого шага зафиксирован тестом по общему spy-логу.

Пока реализованы `propose` ([DESIGN-003]), `verify` ([DESIGN-004]) и
`review` ([DESIGN-005]); остальные шаги приходят своими задачами и делят с
ними `StepContext`.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from disputatio.context import build_author_prompt, build_reviewer_prompt
from disputatio.contracts import (
    Event,
    EventSource,
    EventType,
    Review,
    ReviewAcceptance,
    Role,
    VerificationReport,
    parse_proposal,
    validate_review,
)
from disputatio.core import SessionFsm, Writer, active_writer
from disputatio.events import write_round_artifact
from disputatio.runtime.composition import RuntimeDeps
from disputatio.runtime.errors import ReviewNotAccepted
from disputatio.runtime.git import base_rev
from disputatio.runtime.history import load_prior_round, load_verification
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
)
from disputatio.runtime.parsing import extract_json_object
from disputatio.runtime.retry import run_with_schema_retry
from disputatio.verifier import GateSpec


@dataclass(frozen=True, slots=True)
class StepContext:
    """Всё, что нужно шагу: порты, FSM и цель сброса первого раунда.

    `round` берётся у FSM, а не хранится полем: раунд меняет только переход,
    и вторая копия номера разошлась бы с `session.json` ровно в тот момент,
    когда её никто не проверяет — при resume.

    `base_commit` приходит из снапшота `config.toml` (ADR-003): `HEAD` на
    старте сессии — единственная цель сброса раунда 1, и восстановима она
    только из файла, пережившего перезапуск процесса.

    `gates` — тот же список `GateSpec`, из которого собран `deps.verifier`.
    Он нужен именно здесь, а не внутри верификатора: пакет `verifier` от
    `EventSink` не зависит и `gate_started` эмитить не может, а событие
    «гейт пошёл» обязано уйти в журнал ДО прогона — то есть до того, как у
    оркестратора появится хоть один `GateResult` ([DESIGN-004]).
    """

    deps: RuntimeDeps
    fsm: SessionFsm
    base_commit: str
    gates: tuple[GateSpec, ...] = field(default=())

    @property
    def root(self) -> Path:
        """Рабочий git-репозиторий сессии."""
        return self.deps.root

    @property
    def round(self) -> int:
        """Номер текущего раунда — из состояния, а не из копии."""
        return self.fsm.state.current_round


async def propose(ctx: StepContext) -> None:
    """Шаг PROPOSING раунда `ctx.round`: reset → prompt → author → артефакты.

    Порядок операций — само поведение шага, а не его деталь:

    1. `reset_hard(base_rev(...))` + `clean` — **до** вызова адаптера
       ([REQ-012]). Уборка после автора снесла бы работу автора; уборка
       вместо сброса оставила бы в дереве правки прерванной попытки, и они
       ушли бы ревьюеру как работа этого раунда.
    2. Промпт собирается `context.build_author_prompt` из артефактов раунда
       N−1, прочитанных с диска (§6.1). Прошлых proposal среди них нет —
       источник истины для автора это файлы рабочей директории.
    3. Единственный `await` шага — вызов адаптера.
    4. Ответ разбирается `parse_proposal` **до** записи: `proposal.md` с
       битым фронтматтером на диске означал бы, что следующий раунд читает
       как артефакт то, что артефактом не является. Разбор идёт внутри
       schema-retry ([DESIGN-006]): невалидный ответ — повод переспросить
       автора с текстом ошибки, а не сразу уронить сессию.
    5. `changes.patch` пишется всегда, в том числе пустым ([REQ-013]):
       «автор ничего не менял» и «шаг не дошёл до патча» — разные факты, и
       различает их только наличие файла. `diff_head` идёт ПОСЛЕ записи
       `proposal.md`: каталог сессии из диффа исключён, поэтому порядок
       безопасен, а обратный лишил бы патч правок, сделанных автором позже.
    """
    _require_author(ctx)
    round_no = ctx.round
    root = ctx.root

    ctx.deps.git.reset_hard(base_rev(root, round_no, base_commit=ctx.base_commit))
    ctx.deps.git.clean()

    prior = load_prior_round(root, round_no - 1)
    failures: list[Exception] = []
    outcome = await run_with_schema_retry(
        ctx,
        adapter=ctx.deps.author,
        build_prompt=lambda: build_author_prompt(
            task=ctx.fsm.state.task,
            round=round_no,
            prior_review=prior.review,
            prior_verification=prior.verification,
            prior_decision=prior.decision,
        ),
        parse=parse_proposal,
        source=EventSource.AUTHOR,
        session_ref=_author_session_ref(ctx),
        on_invalid=failures.append,
    )
    if outcome is None:
        raise _exhausted(failures)
    _, turn = outcome

    write_round_artifact(root, round_no, PROPOSAL_NAME, turn.text)
    diff = ctx.deps.git.diff_head()
    write_round_artifact(root, round_no, CHANGES_PATCH_NAME, diff)

    ctx.fsm.handle_step_success()


def verify(ctx: StepContext) -> None:
    """Шаг VERIFYING раунда `ctx.round`: гейт-события, прогон, отчёт.

    `Verifier.verify` синхронен и монолитен: он прогоняет все гейты за один
    вызов и об `EventSink` не знает — пакет `verifier` от порта событий не
    зависит ([REQ-010] там же). Поэтому обрамление — работа runtime:

    1. `gate_started` по каждому `GateSpec` — **до** вызова. Позже было бы
       поздно: UI подписан на `events.jsonl` и обязан показать «идёт
       `pytest`» пока `pytest` идёт, а не после того, как всё кончилось.
    2. `gate_finished` по каждому `GateResult` — после. Пара «спека →
       результат» держится индексом и именем: порядок `report.gates` равен
       порядку конфигурации, и это гарантия `VerifierRunner`, которую здесь
       нельзя нарушить — гейты не сортируются и не фильтруются.
    3. `verification.json` пишется `model_dump_json(by_alias=True)`: поле
       схемы называется `schema`, а атрибут модели — `schema_`.

    `overall == fail` шаг не прерывает и переход `VERIFYING → REVIEWING` не
    отменяет ([REQ-004]): провалившийся гейт — это материал для ревьюера, а
    не приговор раунду. Правило выражено отсутствием кода — в теле шага нет
    ни ветвления, ни `raise`, — а не проверкой: проверку можно было бы
    случайно инвертировать, отсутствующую ветку инвертировать нельзя.
    """
    round_no = ctx.round

    for spec in ctx.gates:
        _emit_gate_event(
            ctx, EventType.GATE_STARTED, {"name": spec.name, "cmd": spec.cmd}
        )

    report = ctx.deps.verifier.verify(round_no)

    for result in report.gates:
        _emit_gate_event(
            ctx,
            EventType.GATE_FINISHED,
            {
                "name": result.name,
                "status": result.status.value,
                "exit_code": result.exit_code,
            },
        )

    write_round_artifact(
        ctx.root,
        round_no,
        VERIFICATION_NAME,
        report.model_dump_json(by_alias=True),
    )


async def review(ctx: StepContext) -> None:
    """Шаг REVIEWING раунда `ctx.round`: промпт → ревьюер → `review.json`.

    Правила §4.4 здесь не переписываются ни одной строкой: деградация
    `blocker|major` без evidence до `minor`, отказ `approve` при
    `verification.overall == fail` и отказ при пустом `checked` — это
    результат ОДНОГО вызова `contracts.validate_review`. Продублируй
    любое из них здесь — и два места начали бы отвечать на один вопрос,
    расходясь ровно тогда, когда §4.4 поправят в одном из них.

    Runtime решает три вещи, и только их:

    1. **Что уходит ревьюеру.** Промпт собирает `build_reviewer_prompt`;
       `proposal.md` и `changes.patch` передаются относительными путями —
       ревьюер read-only (§7) и читает файлы сам, а копия в промпте
       разошлась бы с рабочей директорией. Метки «данные, не инструкции»
       ставит `context`: собственная обёртка runtime разъехалась бы с той,
       на которую рассчитан промпт.
    2. **Что делать с ответом.** `extract_json_object` находит границы
       объекта, `Review.model_validate_json` проверяет схему — и только
       потом §4.4. Ни на одном из трёх шагов текст ревьюера не
       исполняется: он остаётся данными от адаптера до диска (NFR-003).
    3. **Какую модель записать.** На диск идёт `acceptance.review` —
       деградированная копия. Исходная модель сохранила бы `blocker`,
       который §4.4 уже не признал, и следующий раунд читал бы его как
       настоящий.

    Отчёт проверок читается с диска (раунд N, не N−1) — тот же источник,
    что переживёт перезапуск процесса, и тот же, из которого §4.4 узнает
    про красные гейты.

    Все три исхода отказа (нет JSON, не та схема, не приняли §4.4) идут
    через schema-retry ([DESIGN-006]): ревьюера переспрашивают с текстом
    ошибки, и только исчерпание лимита делает раунд `FAILED`.
    """
    round_no = ctx.round
    root = ctx.root

    verification = _round_verification(root, round_no)
    prior = load_prior_round(root, round_no - 1)
    failures: list[Exception] = []
    outcome = await run_with_schema_retry(
        ctx,
        adapter=ctx.deps.reviewer,
        build_prompt=lambda: build_reviewer_prompt(
            task=ctx.fsm.state.task,
            round=round_no,
            proposal_path=_relative_artifact(root, round_no, PROPOSAL_NAME),
            patch_path=_relative_artifact(root, round_no, CHANGES_PATCH_NAME),
            verification=verification,
            prior_review=prior.review,
            prior_decision=prior.decision,
        ),
        parse=lambda text: _accepted_review(text, verification, round_no),
        source=EventSource.REVIEWER,
        session_ref=_reviewer_session_ref(ctx),
        on_invalid=failures.append,
    )
    if outcome is None:
        raise _exhausted(failures)
    review_model, _turn = outcome

    write_round_artifact(
        root,
        round_no,
        REVIEW_NAME,
        review_model.model_dump_json(by_alias=True),
    )

    ctx.fsm.handle_step_success()


def _accepted_review(
    text: str, verification: VerificationReport, round_no: int
) -> Review:
    """Текст ревьюера → принятая §4.4 модель; иначе ошибка для повтора.

    Три исхода отказа — нет JSON, не та схема, не приняли правила §4.4 —
    поднимаются как исключения, потому что для schema-retry ([DESIGN-006])
    это один и тот же факт: вывод агента не той формы, и лечится он
    повтором с текстом ошибки, а не ветвлением здесь.

    Возвращается `acceptance.review` — деградированная копия: исходная
    модель сохранила бы `blocker`, который §4.4 уже не признал, и следующий
    раунд читал бы его как настоящий.
    """
    parsed = Review.model_validate_json(extract_json_object(text))
    acceptance = validate_review(parsed, verification)
    _require_accepted(acceptance, round_no)
    return acceptance.review


def _exhausted(failures: Sequence[Exception]) -> Exception:
    """Ошибка, с которой шаг падает после исчерпания повторов ([REQ-006]).

    Наружу уходит ошибка ПОСЛЕДНЕЙ попытки: сессия к этому моменту уже
    `FAILED` в `session.json`, а все попытки — в журнале событиями `error`,
    но пользователь обязан услышать причину, а не только факт остановки.
    Переписывать её в собственный текст незачем: §4.4 и схема уже сказали,
    что именно не так, и второй формулировкой они бы разошлись.

    Пустой список означает возврат `None` без единой неудачной попытки —
    состояние, невозможное по контракту хелпера, поэтому `AssertionError`.
    """
    if not failures:
        raise AssertionError(
            "schema-retry вернул None, не зафиксировав ни одной ошибки "
            "валидации: счёт попыток разошёлся с их разбором"
        )
    return failures[-1]


def _round_verification(root: Path, round_no: int) -> VerificationReport:
    """Отчёт проверок раунда `round_no`; его отсутствие — ошибка порядка.

    `AssertionError`, а не доменная ошибка: `VERIFYING` всегда
    предшествует `REVIEWING`, и write-ahead-переход не даёт войти сюда
    раньше, чем отчёт лёг на диск. Значит пустое место здесь означает
    сломанную диспетчеризацию цикла, а не действие пользователя.
    """
    report = load_verification(root, round_no)
    if report is None:
        raise AssertionError(
            f"нет verification.json раунда {round_no:03d}: шаг REVIEWING "
            "вызван до VERIFYING — ревьюер не может судить по гейтам, "
            "которых не прогоняли"
        )
    return report


def _relative_artifact(root: Path, round_no: int, name: str) -> str:
    """Путь артефакта раунда относительно `root`, POSIX-разделителями.

    Относительный — не косметика: абсолютный путь машины оркестратора
    бесполезен ревьюеру, работающему из `root`, и заодно утёк бы в промпт
    раскладкой файловой системы. `as_posix` фиксирует разделитель: промпт
    обязан быть одинаковым на любой ОС (NFR-002).
    """
    return round_artifact(root, round_no, name).relative_to(root).as_posix()


def _require_accepted(acceptance: ReviewAcceptance, round_no: int) -> None:
    """Непринятое §4.4 ревью не пишется на диск, а требует повтора.

    Причины пересылаются как есть — machine-readable кодами
    `contracts.REASON_*`: из них схемный retry ([DESIGN-006]) соберёт
    следующий промпт, и переписывание их в человеческий текст здесь
    сделало бы этот текст вторым источником правды о §4.4.
    """
    if not acceptance.accepted:
        raise ReviewNotAccepted(acceptance.rejection_reasons, round_no=round_no)


def _reviewer_session_ref(ctx: StepContext) -> str | None:
    """`--resume`-ссылка ревьюера; `None` — законный холодный старт (§6.2)."""
    return ctx.fsm.state.agents[Role.REVIEWER].session_ref


def _emit_gate_event(
    ctx: StepContext, event_type: EventType, payload: dict[str, Any]
) -> None:
    """Кладёт в журнал событие гейта §8 от имени `verifier`.

    Источник — `EventSource.VERIFIER`, а не `ORCHESTRATOR`: для подписчика
    важно, чей это поток, а не кто физически дописал строку. `ts` берётся у
    инжектированных часов сессии — второй источник времени сделал бы
    `events.jsonl` недетерминированным в тестах ([REQ-001]).
    """
    ctx.deps.sink.emit(
        Event(
            ts=ctx.deps.now(),
            session=ctx.fsm.state.session_id,
            round=ctx.round,
            source=EventSource.VERIFIER,
            type=event_type,
            payload=payload,
        )
    )


def _require_author(ctx: StepContext) -> None:
    """Инвариант I1: тело шага не начинается, пока писатель не автор.

    Ответ на вопрос «кто вправе писать» берётся у `core.active_writer` —
    собственной таблицы писателей runtime не заводит: две таблицы разошлись
    бы молча, и разошлись бы именно там, где цена ошибки максимальна (§7,
    ревьюер писателем не бывает никогда).

    `AssertionError`, а не доменная ошибка: сюда приводит только ошибка
    диспетчеризации цикла, а не действие пользователя, — а `raise` вместо
    `assert` оставляет проверку живой и под `python -O`.
    """
    writer = active_writer(ctx.fsm.state.state)
    if writer is not Writer.AUTHOR:
        raise AssertionError(
            f"PROPOSING ожидает писателя {Writer.AUTHOR}, а фаза "
            f"{ctx.fsm.state.state} отдаёт {writer}: шаг вызван не из своей фазы"
        )


def _author_session_ref(ctx: StepContext) -> str | None:
    """`--resume`-ссылка автора; `None` — законный холодный старт (§6.1)."""
    return ctx.fsm.state.agents[Role.AUTHOR].session_ref
