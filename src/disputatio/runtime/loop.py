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
  `session.json` не знает.
* `NEXT_PHASE` — куда идти после шага. Записей `DECIDING` и `EXPORTING` тут
  нет, и это часть контракта: следующую фазу после решения выбирает
  `SessionFsm.apply_decision` по §5, после экспорта — сам `export`. Заведи
  их здесь — и порядок стоп-условий получил бы второй источник правды,
  расходящийся с ядром ровно тогда, когда §5 поправят в одном из двух мест.

Отдельной «логики восстановления» в модуле тоже нет ([REQ-014]): `drive`
начинает с фазы сохранённого состояния, поэтому холодный старт и resume
отличаются только тем, откуда пришло начальное `SessionState`.
"""

from collections.abc import Awaitable, Callable, Mapping
from inspect import isawaitable

from disputatio.contracts import SessionPhase, SessionState
from disputatio.core import TERMINAL_PHASES
from disputatio.runtime import steps
from disputatio.runtime.steps import StepContext

StepFn = Callable[[StepContext], Awaitable[None] | None]
"""Тело шага: синхронное (`verify`, `decide_step`) либо ожидаемое.

Разговор с агентом асинхронен, прогон гейтов и решение — нет, и обёртывать
синхронный шаг в корутину ради единообразия значило бы делать вид, что у
него есть точка отмены, которой нет.
"""

STEP_BY_PHASE: Mapping[SessionPhase, StepFn] = {
    SessionPhase.PROPOSING: steps.propose,
    SessionPhase.VERIFYING: steps.verify,
    SessionPhase.REVIEWING: steps.review,
    SessionPhase.DECIDING: steps.decide_step,
}
"""Фаза → её шаг. `EXPORTING` появится здесь вместе с `exporting.export`."""

NEXT_PHASE: Mapping[SessionPhase, SessionPhase] = {
    SessionPhase.IDLE: SessionPhase.PROPOSING,
    SessionPhase.PROPOSING: SessionPhase.VERIFYING,
    SessionPhase.VERIFYING: SessionPhase.REVIEWING,
    SessionPhase.REVIEWING: SessionPhase.DECIDING,
}
"""Безусловные рёбра раунда. `DECIDING`/`EXPORTING` отсутствуют намеренно."""


async def drive(ctx: StepContext) -> SessionState:
    """Крутит сессию от текущей фазы до терминальной ([REQ-008], [REQ-014]).

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

    Возвращается состояние терминальной фазы (`DONE`/`FAILED`); терминальный
    вход — законный: `drive` на уже завершённой сессии не делает ничего.

    Фаза без шага и без ребра — дыра в диспетчере, и она поднимает
    `AssertionError`, а не крутится вечно и не возвращается молча: тихий
    возврат выдал бы за успех сессию, не дошедшую до результата. Ошибка
    именно программная — сюда приводит незарегистрированный шаг, а не
    действие пользователя.
    """
    while ctx.fsm.state.state not in TERMINAL_PHASES:
        phase = ctx.fsm.state.state

        step = STEP_BY_PHASE.get(phase)
        if step is not None:
            await _run_step(step, ctx)

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


async def _run_step(step: StepFn, ctx: StepContext) -> None:
    """Исполняет шаг, дожидаясь его, если он корутина.

    Проверка идёт по результату, а не по таблице «этот шаг асинхронный»:
    вторая таблица разошлась бы с первой молча, и молча же потеряла бы
    `await` — то есть выполнила бы шаг наполовину, отчитавшись об успехе.
    """
    outcome = step(ctx)
    if isawaitable(outcome):
        await outcome
