"""Write-ahead-диспетчер оркестраторного цикла ([DESIGN-008], [REQ-008]).

Ядро восстановимости выражено формой цикла, а не отдельной проверкой: тело
шага не выполняется раньше перехода, поэтому `session.json` всегда называет
шаг, который НАЧИНАЛСЯ, а не тот, что завершился. Обрыв процесса в любой
точке оставляет сессию в фазе, которую resume обязан переиграть целиком
(ADR-001), — и переиграть безопасно, потому что записанного результата у
этой фазы ещё нет.

Сам write-ahead реализован не здесь: `SessionFsm.transition` уже делает
`check → store.save → sink.emit` в этом порядке. Runtime добавляет к нему
ровно одно обязательство — не звать ни один порт шага раньше перехода. Всё,
что этот модуль может испортить, портится перестановкой строк, а не
логикой, поэтому порядок пинится общим spy-логом в
`tests/runtime/test_write_ahead.py`.

Две таблицы, и обе — данные, а не поведение:

* `STEP_BY_PHASE` — чем занята фаза. `IDLE` шага не имеет: её работа и есть
  переход, и работа до `save(PROPOSING)` была бы работой, о которой
  `session.json` не знает. Остальные фазы, включая `EXPORTING`, свой шаг
  здесь имеют — фаза без шага и без ребра останавливает сессию на полпути.
* `NEXT_PHASE` — куда идти после шага. Записей `DECIDING` и `EXPORTING` тут
  нет, и это часть контракта: следующую фазу после решения выбирает
  `SessionFsm.apply_decision` по §5, после экспорта — сам `export`. Заведи
  их здесь — и порядок стоп-условий получил бы второй источник правды,
  расходящийся с ядром ровно тогда, когда §5 поправят в одном из двух мест.

Отдельной «логики восстановления» в модуле тоже нет ([REQ-014]): `drive`
начинает с фазы сохранённого состояния, поэтому холодный старт и resume
отличаются только тем, откуда пришло начальное `SessionState`. Всё, что
добавляет `resume_session`, — это подготовка: снапшот `config.toml` вместо
конфига окружения и `store.load` вместо `config.to_session_state`.
"""

from collections.abc import Awaitable, Callable, Mapping
from inspect import isawaitable
from pathlib import Path
from typing import Any

from disputatio.contracts import (
    AgentTurn,
    BoundaryVerdict,
    RoundBoundaryPolicy,
    SessionLifecyclePolicy,
    SessionPhase,
    SessionState,
)
from disputatio.core import TERMINAL_PHASES, SessionFsm
from disputatio.runtime import exporting, steps
from disputatio.runtime.budget import charge_step
from disputatio.runtime.composition import build_runtime
from disputatio.runtime.config import load_config
from disputatio.runtime.errors import SessionNotFound
from disputatio.runtime.steps import StepContext

StepFn = Callable[[StepContext], Awaitable[AgentTurn | None] | AgentTurn | None]
"""Тело шага: синхронное (`verify`, `decide_step`) либо ожидаемое.

Разговор с агентом асинхронен, прогон гейтов и решение — нет, и обёртывать
синхронный шаг в корутину ради единообразия значило бы делать вид, что у
него есть точка отмены, которой нет.

Возвращает шаг ровно то, что нужно для учёта бюджета ([DESIGN-009]): свой
`AgentTurn`, если агента звал, и `None`, если не звал. Расход считает не шаг,
а граница шага — иначе `store.save` бюджета оказался бы внутри шага, то есть
внутри retry-петли, где новый FSM обнулил бы лимит I4 (ADR-004).
"""

STEP_BY_PHASE: Mapping[SessionPhase, StepFn] = {
    SessionPhase.PROPOSING: steps.propose,
    SessionPhase.VERIFYING: steps.verify,
    SessionPhase.REVIEWING: steps.review,
    SessionPhase.DECIDING: steps.decide_step,
    SessionPhase.EXPORTING: exporting.export,
}
"""Фаза → её шаг; последняя запись закрывает цикл ([REQ-024], [DESIGN-024]).

`EXPORTING` — единственная фаза, чей шаг сам называет следующую (`DONE`),
поэтому в `NEXT_PHASE` её нет и быть не должно. Пока записи не было,
терминальная цепочка §5 приводила сессию в фазу без шага и без ребра, и
`drive` честно падал на дыре диспетчера: сессия, вынесшая решение, не
оставляла ни `result/`, ни `manifest.json`. Регистрация здесь — весь объём
интеграции: сам экспорт живёт в `runtime/exporting.py` и о цикле не знает.
"""

NEXT_PHASE: Mapping[SessionPhase, SessionPhase] = {
    SessionPhase.IDLE: SessionPhase.PROPOSING,
    SessionPhase.PROPOSING: SessionPhase.VERIFYING,
    SessionPhase.VERIFYING: SessionPhase.REVIEWING,
    SessionPhase.REVIEWING: SessionPhase.DECIDING,
}
"""Безусловные рёбра раунда. `DECIDING`/`EXPORTING` отсутствуют намеренно."""


async def resume_session(
    workspace_root: Path,
    session_id: str,
    *,
    artifact_root: Path | None = None,
    round_boundary: RoundBoundaryPolicy | None = None,
    lifecycle: SessionLifecyclePolicy | None = None,
    **overrides: Any,
) -> SessionState:
    """Поднимает сессию с последнего write-ahead перехода ([REQ-014]).

    Собственной «логики восстановления» здесь нет и быть не должно — есть
    четыре строки подготовки и тот же `drive`, что крутит холодный старт.
    Отличие ровно одно: начальное `SessionState` приходит из `session.json`,
    а не из `config.to_session_state`. Заведись у resume хоть один свой шаг
    («пропустить фазу», «переиграть раунд», «подтянуть конфиг»), и
    восстановленная сессия перестала бы быть той же самой сессией.

    `artifact_root` — журнал сессии (SPEC-002 §4.1); `None` означает
    «журнал в рабочем репозитории», то есть путь до разделения. Параметр
    объявлен ЗДЕСЬ, а не только у `build_runtime`, потому что снапшот
    читается ДО сборки портов: доедь `artifact_root` до сессии только через
    `overrides`, и `load_config` всё равно смотрел бы в рабочий корень —
    вложенная сессия падала бы `ConfigError` на чужом (или отсутствующем)
    снапшоте, ещё не дойдя до собственного состояния.

    Порядок подготовки значим дважды:

    1. **Конфиг — из снапшота сессии**, а не из текущего окружения
       ([DESIGN-014]). Внешний `config.toml` мог измениться между запусками,
       и сессия, продолженная с другими лимитами, гейтами или `base_commit`,
       противоречила бы уже записанным раундам — в частности, цель сброса
       раунда 1 восстановима только из снапшота.
    2. **Состояние — после сборки портов**: `store` берётся у собранных
       зависимостей, чтобы resume читал сессию тем же хранилищем, которым
       цикл будет её писать. Второй источник состояния разошёлся бы с
       первым молча.

    `KeyError` от `store.load` переводится в `SessionNotFound`: отсутствие
    сессии — ошибка пользователя, и CLI обязан напечатать её строкой, а не
    repr'ом ключа в кавычках ([DESIGN-020]). Нечитаемый снапшот приходит
    `ConfigError` оттуда же, из `load_config`.

    `round_boundary` и `lifecycle` — прокладка до `drive` (SPEC-002 §7.1):
    политики принадлежат вызывающему циклу, а не сборке портов, и в
    `overrides` попасть не должны — `build_runtime` их не знает. Дефолт
    `None` оставляет resume ровно тем, чем он был.

    Остальные `overrides` передаются в `build_runtime` как есть: подмена
    любого порта фейком не требует ни отдельного пути, ни правок цикла
    ([REQ-001]).
    """
    journal_root = artifact_root if artifact_root is not None else workspace_root
    config = load_config(journal_root)
    deps = build_runtime(
        config, workspace_root, artifact_root=journal_root, **overrides
    )
    try:
        state = deps.store.load(session_id)
    except KeyError as exc:
        raise SessionNotFound(
            f"сессии {session_id!r} нет в {journal_root}: session.json "
            "отсутствует либо принадлежит другой сессии"
        ) from exc
    fsm = SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now)
    return await drive(
        StepContext(
            deps=deps,
            fsm=fsm,
            base_commit=config.base_commit,
            gates=config.gates,
        ),
        round_boundary=round_boundary,
        lifecycle=lifecycle,
    )


async def drive(
    ctx: StepContext,
    *,
    round_boundary: RoundBoundaryPolicy | None = None,
    lifecycle: SessionLifecyclePolicy | None = None,
) -> SessionState:
    """Крутит сессию от текущей фазы до терминальной ([REQ-008], [REQ-014]).

    Обе политики (SPEC-002 §7.1) опциональны и по умолчанию отсутствуют:
    без них цикл идёт тем же путём, что и до пайплайна, — ни одной лишней
    ветки, ни одного лишнего чтения с диска. `spec`-контур гонит сессию до
    её собственного терминала и не передаёт ни одной.

    `round_boundary` опрашивается на границе раунда: после того, как
    `REVIEWING` положил `review.json` на диск, и ДО `decide()`. Точка
    выбрана так, а не после ветки `CONTINUE`: `decide()` идёт строго
    top-down (`core/deciding.py`), и на последнем разрешённом раунде или
    при исчерпанном бюджете он вернул бы `DEADLOCK`/`BUDGET_HIT` раньше
    `CONTINUE` — политика не была бы опрошена вовсе, а архитектурная
    находка ушла бы в эскалацию вместо обязательного возврата к спеке (P6).
    `PARK` означает: `decide()` не вызывается, `decision.json` раунда не
    пишется, `drive` возвращает управление с текущим нетерминальным
    состоянием (`DECIDING`) — на отсутствии решения §8.1 и строит identity
    припаркованного checkpoint'а.

    `lifecycle` уезжает в контекст, потому что зовёт его не цикл, а шаг
    автора — точнее, `run_with_schema_retry` вокруг каждого вызова адаптера
    (P9). Цикл здесь только доставляет политику до шага.

    Итерация читается сверху вниз и вся состоит из порядка:

    1. **Шаг текущей фазы** — если он у неё есть. Фаза уже сохранена: либо
       переходом прошлой итерации, либо тем `save`, что пережил обрыв и
       поднял сессию сюда. Поэтому начать с тела шага — не нарушение
       write-ahead, а его следствие.
    2. **Переход по `NEXT_PHASE`** — после шага, а не до: `save` новой фазы
       обязан случиться раньше первого обращения к её портам, и порядок
       «шаг, затем переход» даёт это без единой проверки.
    3. **Фазы вне таблицы** двигает не диспетчер: `decide_step` уходит через
       `apply_decision` в revise-петлю или терминальную цепочку §5. Поэтому
       следующая итерация исполняет шаг НОВОЙ фазы — раунд N+1 начинается с
       предложения автора, а не с гейтов по патчу раунда N.

    Контекст переприсваивается результатом шага: начисление бюджета
    пересаживает сессию на новый `SessionFsm` ([DESIGN-009]), и держать
    после этого прежний означало бы продолжать цикл состоянием без расхода —
    молча и с виду успешно, до первого стоп-условия §5.2.

    Возвращается состояние терминальной фазы (`DONE`/`FAILED`); терминальный
    вход — законный: `drive` на уже завершённой сессии не делает ничего.

    Фаза без шага и без ребра — дыра в диспетчере, и она поднимает
    `AssertionError`, а не крутится вечно и не возвращается молча: тихий
    возврат выдал бы за успех сессию, не дошедшую до результата. Ошибка
    именно программная — сюда приводит незарегистрированный шаг, а не
    действие пользователя.
    """
    if lifecycle is not None:
        ctx = ctx.with_lifecycle(lifecycle)

    while ctx.fsm.state.state not in TERMINAL_PHASES:
        phase = ctx.fsm.state.state

        if phase is SessionPhase.DECIDING and _parks(round_boundary, ctx):
            return ctx.fsm.state

        step = STEP_BY_PHASE.get(phase)
        if step is not None:
            ctx = await _run_step(step, ctx)

        next_phase = NEXT_PHASE.get(phase)
        if next_phase is not None:
            ctx.fsm.transition(next_phase)
        elif ctx.fsm.state.state is phase:
            raise AssertionError(
                f"фаза {phase.value} не имеет ни шага, ни перехода: цикл "
                "остановиться не вправе, а двигаться ему некуда — "
                "диспетчер неполон"
            )

    return ctx.fsm.state


def _parks(policy: RoundBoundaryPolicy | None, ctx: StepContext) -> bool:
    """Опрашивает политику границы раунда; `True` — цикл обязан вернуться.

    Ревью читается тем же `steps.round_review`, которым его читает снимок
    `DECIDING`: собственный разбор артефакта разошёлся бы с ядром ровно
    тогда, когда политика паркует сессию по находке, которой решение не
    видело. Отсутствие политики — ни чтения, ни вопроса: дефолтный путь
    цикла не трогает диск ни на байт больше прежнего.
    """
    if policy is None:
        return False
    review = steps.round_review(ctx.artifact_root, ctx.round)
    return policy.after_deciding(review) is BoundaryVerdict.PARK


async def _run_step(step: StepFn, ctx: StepContext) -> StepContext:
    """Исполняет шаг, замеряет его время и начисляет бюджет ([DESIGN-009]).

    Проверка «корутина ли» идёт по результату, а не по таблице «этот шаг
    асинхронный»: вторая таблица разошлась бы с первой молча, и молча же
    потеряла бы `await` — то есть выполнила бы шаг наполовину, отчитавшись об
    успехе.

    Замер обнимает всё тело шага, а не только разговор с агентом: `wall_seconds`
    — это стена сессии (§5.2), и гейты с записью артефактов идут в неё наравне.
    Оба отсчёта берутся у ОДНИХ инжектированных часов `deps.monotonic`, поэтому
    разница неотрицательна и не зависит ни от системного времени, ни от того,
    как долго шаг ждал ввода-вывода.

    Начисление идёт ПОСЛЕ шага — то есть после `handle_step_success` внутри
    него: сюда управление приходит только у шага, дошедшего до конца, и
    обнулить лимит I4 посреди retry-петли эта граница не может по построению
    (ADR-004). Упавший шаг бюджета не начисляет вовсе: исключение уходит
    наружу мимо этой строки.
    """
    started = ctx.deps.monotonic()
    outcome = step(ctx)
    turn = await outcome if isawaitable(outcome) else outcome
    return charge_step(ctx, turn=turn, elapsed_s=ctx.deps.monotonic() - started)
