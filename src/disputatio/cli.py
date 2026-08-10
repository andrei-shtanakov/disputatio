"""`disp` — командный вход оркестратора ([REQ-019], [DESIGN-019], ADR-007).

Один модуль на весь CLI, stdlib `argparse` и ни одной новой зависимости, а
`main(argv) -> int` делает вход обычной функцией — тест вызывает её
напрямую, не порождая процесса и не завися от того, установлен ли пакет.
Подкоманда пока одна (`run`); `resume` приходит с [TASK-021] и садится
рядом, не трогая ни порядок ниже, ни коды возврата.

Единственное, что этот модуль решает сам, — **порядок** и **коды возврата**.
Ни одного правила сессии здесь нет: pre-flight принадлежит `runtime.git`,
сборка портов — composition root'у, стоп-условия — ядру. Порядок же
принадлежит именно CLI, и он обязателен:

1. **Все проверки старта — до первой записи.** `preflight(root)`
   ([DESIGN-010]), чтение профиля и сборка портов идут раньше
   `bootstrap_session`: отказ любой из них не вправе оставить `.disputatio/`
   в чужом репозитории ([REQ-010]), а единственный способ этого добиться —
   не создавать каталог, пока не прошла последняя из проверок. Грязное
   дерево тут не единственный отказ: адаптер, которого нет в реестре,
   отвергается `build_runtime`, и порядок «bootstrap раньше сборки» оставлял
   бы после опечатки в профиле каталог сессии, которой не было.
2. `session_id` печатается **до** первого записанного файла и первой строки
   журнала, но **после** всех проверок: сессия, которую пользователь не
   может назвать, не поддаётся ни `disp resume`, ни разбору инцидента — а
   названная, но не начавшаяся, посылает его искать несуществующее.
3. Цикл крутится `anyio.run`, а не `asyncio.run` — правило проекта и
   требование порта `AgentAdapter.run`.

`config.toml` сессии — это профиль запуска, в котором заменены ровно четыре
поля, принадлежащие ЭТОМУ запуску: `session.id`, `session.mode`,
`session.base_commit` и `task.prompt`. Формат профиля и снапшота один и тот
же ([DESIGN-014]), поэтому конфиг завершённой сессии годится как профиль
следующей, а читатель и писатель остаются в одном экземпляре. Всё остальное
— агенты, лимиты, гейты — приходит из профиля нетронутым: подкрути их CLI, и
снапшот перестал бы описывать сессию, которая по нему пошла.
"""

import argparse
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import anyio

from disputatio.contracts import Mode, SessionPhase, SessionState
from disputatio.core import SessionFsm
from disputatio.events import bootstrap_session, write_config_snapshot
from disputatio.runtime import (
    DisputatioError,
    GitCli,
    base_rev,
    build_runtime,
    load_config_file,
    preflight,
)
from disputatio.runtime.loop import drive
from disputatio.runtime.steps import StepContext

EXIT_OK: Final = 0
"""Сессия дошла до `DONE` — в том числе эскалацией: она не сбой ([REQ-018])."""

EXIT_FAILED: Final = 1
"""Сессия записала `FAILED`: агент так и не вернул пригодный вывод."""

EXIT_ERROR: Final = 2
"""Сессия не началась: доменная ошибка запуска либо негодные аргументы."""

SESSION_ID_TIME_FORMAT: Final = "%Y%m%d-%H%M%S"
SESSION_ID_SUFFIX_BYTES: Final = 2
"""Два байта — четыре hex-символа суффикса `session_id` ([DESIGN-019])."""

DEFAULT_CONFIG_NAME: Final = "disputatio.toml"
"""Профиль запуска по умолчанию — рядом с рабочим репозиторием."""

_FIRST_ROUND: Final = 1
_HEAD_REVISION: Final = "HEAD"


def main(
    argv: Sequence[str] | None = None,
    *,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Точка входа `disp`: код возврата вместо исключения (ADR-007).

    Доменная ошибка печатается одной строкой в stderr и уходит кодом `2`:
    голого traceback пользователь не видит никогда (NFR-003). Недоменное
    исключение, наоборот, не глушится — это баг оркестратора, а не ошибка
    пользователя, и прятать его за тем же кодом значило бы выдать поломку за
    отказ во вводе.

    `now` инжектируется теми же соображениями, что и `RuntimeDeps.now`: часы
    сессии обязаны быть одни на весь запуск, а тест — детерминированным.
    """
    args = _build_parser().parse_args(argv)
    clock = now if now is not None else _utcnow
    try:
        return cmd_run(args, now=clock)
    except DisputatioError as exc:
        print(exc.args[0], file=sys.stderr)
        return EXIT_ERROR


def cmd_run(args: argparse.Namespace, *, now: Callable[[], datetime]) -> int:
    """Новая сессия: pre-flight → bootstrap → drive ([REQ-019], [DESIGN-019]).

    Порядок обязателен: pre-flight git ([DESIGN-010]) и сборка портов
    выполняются ДО `bootstrap_session`, поэтому ни грязное дерево, ни
    негодный профиль не оставляют `.disputatio/` ([REQ-010]). Сборка портов
    ничего не пишет — она лишь разрешает имена адаптеров в реестре, и
    единственный её отказ (`UnknownAdapterError`/`ConfigError`) относится к
    старту, а не к сессии.

    Начальное состояние сохраняется отдельным `store.save` до цикла: сессия,
    названная в stdout, обязана существовать в `session.json` независимо от
    того, дойдёт ли первый шаг до своего перехода. Событие `state_change`
    при этом не выдумывается — его эмитит первый же переход `IDLE →
    PROPOSING` внутри FSM, и второе, сочинённое здесь, разошлось бы с §8.
    """
    root = Path(args.root)
    preflight(root)

    profile = load_config_file(_config_path(args, root))
    started = now()
    config = replace(
        profile,
        session_id=new_session_id(started),
        mode=Mode(args.mode),
        # `base_commit` — цель сброса раунда 1, то есть `HEAD` на старте
        # сессии ([DESIGN-014]). Считается тем же `base_rev`, которым
        # пользуется шаг PROPOSING: собственный `rev-parse` дал бы второй
        # ответ на вопрос «что такое база сессии».
        base_commit=base_rev(root, _FIRST_ROUND, base_commit=_HEAD_REVISION),
        task_prompt=args.task,
    )
    deps = build_runtime(config, root, git=GitCli(root), now=now)

    # `flush` обязателен: за печатью идёт цикл, который живёт минутами, а в
    # конвейере stdout буферизуется — имя сессии дошло бы до пользователя
    # только вместе с её концом, то есть ровно тогда, когда оно уже не нужно.
    print(config.session_id, flush=True)

    bootstrap_session(root)
    write_config_snapshot(root, config.render_toml())

    state = config.to_session_state(created_at=started)
    deps.store.save(state)
    context = StepContext(
        deps=deps,
        fsm=SessionFsm(state, store=deps.store, sink=deps.sink, now=deps.now),
        base_commit=config.base_commit,
        gates=config.gates,
    )
    return _exit_code(_drive_to_terminal(context))


def new_session_id(moment: datetime) -> str:
    """`{UTC:%Y%m%d-%H%M%S}-{4 hex}` — имя сессии ([DESIGN-019]).

    Штамп переводится в UTC, а не печатается как есть: `session_id` попадает
    в имена артефактов и в journal, и две сессии, запущенные в разных зонах,
    иначе сортировались бы не по времени. Четыре hex-символа отделяют
    сессии, стартовавшие в одну секунду, — без них второй запуск переписал бы
    состояние первого.
    """
    stamp = moment.astimezone(UTC).strftime(SESSION_ID_TIME_FORMAT)
    return f"{stamp}-{secrets.token_hex(SESSION_ID_SUFFIX_BYTES)}"


def _build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов `disp`; подкоманда обязательна ([DESIGN-020])."""
    parser = argparse.ArgumentParser(
        prog="disp", description="Оркестратор author↔reviewer debate loop"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="запустить новую сессию")
    run.add_argument("task", help="текст задачи для автора")
    run.add_argument(
        "--mode",
        choices=[mode.value for mode in Mode],
        default=Mode.DEVELOP.value,
        help="режим задачи (по умолчанию develop)",
    )
    run.add_argument(
        "--config",
        default=None,
        help=f"профиль запуска (по умолчанию <root>/{DEFAULT_CONFIG_NAME})",
    )
    run.add_argument(
        "--root", default=".", help="рабочий git-репозиторий (по умолчанию текущий)"
    )
    return parser


def _config_path(args: argparse.Namespace, root: Path) -> Path:
    """Путь профиля запуска: `--config`, иначе дефолт рядом с репозиторием."""
    if args.config is not None:
        return Path(args.config)
    return root / DEFAULT_CONFIG_NAME


def _drive_to_terminal(context: StepContext) -> SessionState:
    """Крутит цикл под `anyio.run`, отличая провал сессии от поломки CLI.

    Шаг, исчерпавший schema-повторы, поднимает ошибку последней попытки
    ([DESIGN-006]) — но `FAILED` к этому моменту уже записан в `session.json`
    ядром, то есть исход сессии определён, а исключение лишь называет его
    причину. Любое другое исключение — не исход, а сбой, и оно уходит выше:
    проглоти его CLI, и сломанный оркестратор отчитывался бы «сессия не
    сошлась» вместо падения.
    """

    async def call() -> SessionState:
        return await drive(context)

    try:
        return anyio.run(call)
    except Exception:
        if context.fsm.state.state is SessionPhase.FAILED:
            return context.fsm.state
        raise


def _exit_code(state: SessionState) -> int:
    """`0` для `DONE`, `1` для всего остального терминального ([REQ-019])."""
    return EXIT_OK if state.state is SessionPhase.DONE else EXIT_FAILED


def _utcnow() -> datetime:
    """Часы сессии по умолчанию: aware-UTC, как требует схема артефактов."""
    return datetime.now(UTC)
