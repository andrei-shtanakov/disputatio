"""Шаги оркестраторного цикла ([DESIGN-003]…[DESIGN-007]).

Шаг — это порядок вызовов и I/O, и ничего больше: стоп-условия §5 живут в
`core.decide`, граф §2 — в `core.SessionFsm`, валидация §4.4 — в
`contracts.validate_review`, раскладка секций §6 — в `context`. Всё, что
здесь можно испортить, портится перестановкой строк, а не логикой, — поэтому
порядок операций каждого шага зафиксирован тестом по общему spy-логу.

Общее у всех четырёх — начало: шаг переигрывается целиком ([REQ-015],
[DESIGN-015]), поэтому первым делом он убирает огрызки прерванной попытки
(`_purge_partial_artifacts`), а не пытается их доиспользовать.

Общее у всех четырёх и окончание: шаг отдаёт наружу свой `AgentTurn`, если
агента звал, и `None`, если не звал. Расход этого turn'а начисляет граница
шага ([DESIGN-009]) — сам шаг бюджета не считает и `session.json` из-за него
не переписывает: запись изнутри шага попала бы в retry-петлю, где пересадка
FSM обнулила бы лимит schema-повторов (ADR-004).

Пока реализованы `propose` ([DESIGN-003]), `verify` ([DESIGN-004]),
`review` ([DESIGN-005]) и `decide_step` ([DESIGN-007]); остальные шаги
приходят своими задачами и делят с ними `StepContext`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from disputatio.context import (
    build_author_prompt,
    build_doc_author_prompt,
    build_doc_reviewer_prompt,
    build_reviewer_prompt,
)
from disputatio.contracts import (
    SCHEMA_V1,
    SCHEMA_V2,
    AgentTurn,
    Decision,
    Event,
    EventSource,
    EventType,
    Mode,
    ResolvedChecklist,
    Review,
    Role,
    SessionLifecyclePolicy,
    VerificationReport,
    parse_proposal,
    validate_doc_review,
    validate_review,
)
from disputatio.core import (
    DecidingInputs,
    SessionFsm,
    Writer,
    active_writer,
    decide,
    is_partial,
)
from disputatio.events import (
    RoundImmutableError,
    finalize_round,
    write_round_artifact,
)
from disputatio.runtime.composition import RuntimeDeps
from disputatio.runtime.errors import ReviewNotAccepted
from disputatio.runtime.git import base_rev
from disputatio.runtime.history import (
    PriorRound,
    carried_issues,
    issue_history,
    load_adopted_findings,
    load_decision,
    load_patch,
    load_prior_round,
    load_review,
    load_verification,
)
from disputatio.runtime.layout import (
    CHANGES_PATCH_NAME,
    DECISION_NAME,
    PROPOSAL_NAME,
    REVIEW_NAME,
    VERIFICATION_NAME,
    round_artifact,
    round_dir,
)
from disputatio.runtime.parsing import extract_json_object
from disputatio.runtime.retry import run_with_schema_retry
from disputatio.verifier import GateSpec

TEMP_ARTIFACT_PATTERNS: Final = ("*.tmp", "*~")
"""Шаблоны огрызков записи, которые шаг убирает перед своим телом.

`*.tmp` — то, что оставляет `events.atomic_write`, оборванный между
`mkstemp` и `os.replace`: временный файл лежит рядом с целью и её имени не
носит. `*~` — резервная копия редактора; в `rounds/NNN/` она попадает,
только если каталог сессии кто-то открывал руками, но раунд обязан
состоять из артефактов, а не из следов.

Ни один артефакт раунда и ни маркер `.finalized` под эти шаблоны не
подходят, и это не совпадение, а условие: уборка мусора, отменяющая I3,
была бы хуже мусора ([REQ-016]).
"""


@dataclass(frozen=True, slots=True)
class DocSessionSpec:
    """Контур, документы и чеклист doc-сессии — вход промптов §5.1/§5.2.

    Три поля, и ни одного из них нет в `session.json`. `contour` определяет
    задачу автора и набор id, по которому судят ревьюера; `doc_paths` — пара
    документов, которую ревизия видит (spec-контур смотрит спеку, pair-контур
    сверяет план со спекой); `checklist` — ДЕЙСТВУЮЩИЕ формулировки условий
    сходимости, id → текст. Все три приходят от вызывающего `drive`, а не из
    сборки портов: сессия develop/analyze их не имеет вовсе, и дефолт `None`
    оставляет её путь байт-в-байт прежним.

    `checklist` — РАЗРЕШЁННЫЙ чеклист (`ResolvedChecklist`), а не карта
    «id → текст»: состав, порядок и назначенная роль findings-item приходят
    одним объектом. Иначе состав пришлось бы восстанавливать по имени
    контура из глобального каталога — а у операторского контура `doc`
    такого каталога нет вовсе, и порядок его пунктов задаёт конфиг (§5.3).

    `contour` — свободная строка: контуров три (`spec`, `pair`, `doc`), и
    закрытый `Literal` здесь только повторял бы таблицу, которая живёт в
    `CONTOURS_BY_KIND`.
    """

    contour: str
    doc_paths: tuple[str, ...]
    checklist: ResolvedChecklist


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

    Корней у шага два, и берутся они порознь (SPEC-002 §4.1): git идёт по
    `workspace_root`, артефакты и история — по `artifact_root`. Единого
    `root` здесь нет намеренно: пока имя было одно, выбор корня не был
    решением, и вызывающий не мог перепутать их иначе как молча.

    `lifecycle` — политика P9 (SPEC-002 §7.1), которой `PROPOSING` обрамляет
    ход автора. Живёт она в контексте, а не в `RuntimeDeps`, потому что
    приходит от вызывающего `drive`, а не от сборки портов: спека
    (`spec`-контур) её не передаёт вовсе, пара — передаёт. `None` — no-op,
    то есть путь до пайплайна байт-в-байт.

    `documents` — контур, документы и действующий чеклист doc-сессии
    (SPEC-002 §5.1, §5.3). Тоже от вызывающего и тоже с дефолтом `None`:
    `disp run` doc-сессий не заводит, и без него ни одна строка шага не
    меняется.
    """

    deps: RuntimeDeps
    fsm: SessionFsm
    base_commit: str
    gates: tuple[GateSpec, ...] = field(default=())
    lifecycle: SessionLifecyclePolicy | None = None
    documents: DocSessionSpec | None = None

    @property
    def workspace_root(self) -> Path:
        """Рабочий git-репозиторий сессии: сброс, дифф, коммит раунда."""
        return self.deps.workspace_root

    @property
    def artifact_root(self) -> Path:
        """Журнал сессии: `.disputatio/` со состоянием, раундами, экспортом."""
        return self.deps.artifact_root

    @property
    def round(self) -> int:
        """Номер текущего раунда — из состояния, а не из копии."""
        return self.fsm.state.current_round

    def with_fsm(self, fsm: SessionFsm) -> "StepContext":
        """Тот же контекст с другим `SessionFsm` — пересадка [DESIGN-009].

        Бюджет обновляется не мутацией, а сменой FSM: `SessionState` frozen,
        публичного мутатора у ядра нет. Копия живёт здесь, рядом со списком
        полей, а не у того, кто пересаживает: поля перечислены руками, и
        забыть новое проще всего именно вдалеке от их объявления.

        Руками, а не `dataclasses.replace`, по внешней причине: имя `replace`
        в runtime занято сканером append-only ([DESIGN-016]) — отличить
        копию dataclass'а от `Path.replace` он не может и обязан замечать
        каждый вызов, а исключение в разрешающем списке писателей ради копии
        контекста было бы худшей сделкой, чем четыре имени поля.
        """
        return StepContext(
            deps=self.deps,
            fsm=fsm,
            base_commit=self.base_commit,
            gates=self.gates,
            lifecycle=self.lifecycle,
            documents=self.documents,
        )

    def with_lifecycle(self, lifecycle: "SessionLifecyclePolicy") -> "StepContext":
        """Тот же контекст с политикой жизненного цикла хода автора (§7.1).

        Копия по тому же списку полей и по той же причине, что и
        `with_fsm`: `StepContext` frozen, а политику подаёт вызывающий
        `drive`, а не сборка портов.
        """
        return StepContext(
            deps=self.deps,
            fsm=self.fsm,
            base_commit=self.base_commit,
            gates=self.gates,
            lifecycle=lifecycle,
            documents=self.documents,
        )


async def propose(ctx: StepContext) -> AgentTurn:
    """Шаг PROPOSING раунда `ctx.round`: reset → prompt → author → артефакты.

    Порядок операций — само поведение шага, а не его деталь:

    1. `reset_hard(base_rev(...))` + `clean` — **до** вызова адаптера
       ([REQ-012]). Уборка после автора снесла бы работу автора; уборка
       вместо сброса оставила бы в дереве правки прерванной попытки, и они
       ушли бы ревьюеру как работа этого раунда.
    2. Промпт собирается `context.build_author_prompt` из артефактов раунда
       N−1, прочитанных с диска (§6.1). Прошлых proposal среди них нет —
       источник истины для автора это файлы рабочей директории. В
       `Mode.DOCUMENT` сборщик другой (`build_doc_author_prompt`, §5.1
       SPEC-002), но правило то же: документы называются путями, а не
       содержимым, и прошлых их версий автор не получает.
    3. Единственный `await` шага — вызов адаптера. Политика `ctx.lifecycle`
       уходит в `run_with_schema_retry`, а не обнимает шаг здесь: ходов
       автора внутри одного `PROPOSING` столько, сколько попыток у
       schema-retry, а P9 требует снапшот перед КАЖДЫМ (SPEC-002 §7.1).
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

    Возвращается `AgentTurn` принятой попытки — его расход начислит граница
    шага ([DESIGN-009]). Считать бюджет здесь значило бы поставить
    `store.save` внутрь шага, то есть внутрь retry-петли, где новый FSM
    обнулил бы лимит I4 (ADR-004).
    """
    _require_author(ctx)
    round_no = ctx.round
    workspace = ctx.workspace_root
    artifacts = ctx.artifact_root

    _purge_partial_artifacts(artifacts, round_no)
    ctx.deps.git.reset_hard(base_rev(workspace, round_no, base_commit=ctx.base_commit))
    ctx.deps.git.clean()

    prior = load_prior_round(artifacts, round_no - 1)
    failures: list[Exception] = []
    outcome = await run_with_schema_retry(
        ctx,
        adapter=ctx.deps.author,
        build_prompt=lambda: _author_prompt(ctx, round_no, prior),
        parse=parse_proposal,
        source=EventSource.AUTHOR,
        session_ref=_author_session_ref(ctx),
        on_invalid=failures.append,
        lifecycle=ctx.lifecycle,
    )
    if outcome is None:
        raise _exhausted(failures)
    _, turn = outcome

    write_round_artifact(artifacts, round_no, PROPOSAL_NAME, turn.text)
    diff = ctx.deps.git.diff_head()
    write_round_artifact(artifacts, round_no, CHANGES_PATCH_NAME, diff)

    ctx.fsm.handle_step_success()
    return turn


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

    _purge_partial_artifacts(ctx.artifact_root, round_no)
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
        ctx.artifact_root,
        round_no,
        VERIFICATION_NAME,
        report.model_dump_json(by_alias=True),
    )


async def review(ctx: StepContext) -> AgentTurn:
    """Шаг REVIEWING раунда `ctx.round`: промпт → ревьюер → `review.json`.

    Правила §4.4 здесь не переписываются ни одной строкой: деградация
    `blocker|major` без evidence до `minor`, отказ `approve` при
    `verification.overall == fail` и отказ при пустом `checked` — это
    результат ОДНОГО вызова `contracts.validate_review`. Продублируй
    любое из них здесь — и два места начали бы отвечать на один вопрос,
    расходясь ровно тогда, когда §4.4 поправят в одном из них. То же и с
    правилами V1–V8 doc-ревью (§5.2 SPEC-002): их считает
    `contracts.validate_doc_review`, а `_accepted_review` только соблюдает
    порядок вызова, от которого они зависят.

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

    Возвращается `AgentTurn` принятой попытки — его расход начислит граница
    шага ([DESIGN-009]), по той же причине, что и у автора: внутри шага
    начисление попало бы в retry-петлю и обнулило бы лимит I4 (ADR-004).
    """
    round_no = ctx.round
    artifacts = ctx.artifact_root

    _purge_partial_artifacts(artifacts, round_no)
    verification = _round_verification(artifacts, round_no)
    prior = load_prior_round(artifacts, round_no - 1)
    failures: list[Exception] = []
    outcome = await run_with_schema_retry(
        ctx,
        adapter=ctx.deps.reviewer,
        build_prompt=lambda: _reviewer_prompt(ctx, round_no, prior, verification),
        parse=lambda text: _accepted_review(ctx, text, verification, round_no),
        source=EventSource.REVIEWER,
        session_ref=_reviewer_session_ref(ctx),
        on_invalid=failures.append,
    )
    if outcome is None:
        raise _exhausted(failures)
    review_model, turn = outcome

    write_round_artifact(
        artifacts,
        round_no,
        REVIEW_NAME,
        review_model.model_dump_json(by_alias=True),
    )

    ctx.fsm.handle_step_success()
    return turn


def decide_step(ctx: StepContext) -> None:
    """Шаг DECIDING раунда `ctx.round`: снимок → ядро → артефакт → переход.

    Сам шаг ничего не решает. Порядок стоп-условий §5 целиком принадлежит
    `core.decide`, терминальные цепочки §2 — `SessionFsm.apply_decision`, и
    здесь нет ни одной ветки, повторяющей их: собственное мнение об исходе
    разошлось бы с ядром ровно тогда, когда §5 поправят в одном из двух
    мест. Runtime отвечает за три вещи, и все три — про порядок:

    1. **Снимок собран с диска.** `DecidingInputs` — единственный вход
       ядра, и читается он из артефактов, а не из памяти процесса: после
       перезапуска оркестратора решение обязано получиться то же самое.
    2. **`decision.json` записан ДО перехода.** Переход — точка, после
       которой resume считает раунд решённым; артефакт, отставший от неё,
       означал бы раунд без записанного исхода.
    3. **Принятая работа зафиксирована ДО перехода.** `finalize_round`
       закрывает раунд от правок (I3 [REQ-016]), `commit_round` даёт
       следующему раунду цель сброса ([REQ-011], [DESIGN-012]). Случись
       обрыв между переходом и коммитом — раунду N+1 не на что было бы
       сбрасываться, и сессия встала бы намертво. Обратное окно — обрыв
       между маркером и переходом — закрыто `_write_decision`: повтор
       прерванного шага обязан дойти до конца ([REQ-015]).

    Частичный исход (`core.is_partial`) не финализируется и не коммитится:
    это эскалация пользователю (§2.5), и принять такую работу вправе
    только он. Какой исход частичен, знает ядро — собственной таблицы
    исходов runtime не заводит.

    `Decision` материализуется здесь же, а не берётся у `apply_decision`:
    тот отдаёт её уже после переходов, то есть после точки, до которой
    артефакт обязан лежать на диске. Обе материализации собираются из
    одного `DecisionDraft` и одного номера раунда, и их равенство пинится
    тестом шага.
    """
    round_no = ctx.round
    artifacts = ctx.artifact_root

    _purge_partial_artifacts(artifacts, round_no)
    draft = decide(_deciding_inputs(ctx))
    decision = Decision(
        # Тег схемы выбирается режимом сессии: §5.1 SPEC-002 требует, чтобы
        # артефакты doc-сессии несли `disputatio/v2`. `decision.json` своих
        # v2-полей не имеет, но тег описывает семейство артефакта, а не
        # набор заполненных полей — v1 в doc-раунде читался бы как «сессия
        # develop», и sha-сверка версий разошлась бы с `session.json`.
        schema=SCHEMA_V2 if ctx.fsm.state.task.mode is Mode.DOCUMENT else SCHEMA_V1,
        round=round_no,
        outcome=draft.outcome,
        reason=draft.reason,
        open_issues_carried=list(draft.open_issues_carried),
        next_round_directive=draft.next_round_directive,
    )
    _write_decision(artifacts, round_no, decision)

    if not is_partial(draft.outcome):
        finalize_round(artifacts, round_no)
        ctx.deps.git.commit_round(round_no)

    ctx.fsm.apply_decision(draft)


def _purge_partial_artifacts(artifact_root: Path, round_no: int) -> None:
    """Убирает огрызки прерванной записи из `rounds/NNN/` ([REQ-015]).

    `events.atomic_write` обещает атомарность ОДНОЙ записи, а не уборку
    после обрыва посреди неё: временный файл создаётся рядом с целью, и при
    падении процесса между `mkstemp` и `os.replace` он остаётся на диске —
    так и задокументировано. Обещание «временных файлов не остаётся в
    `rounds/NNN/`» даёт не писатель, а тот, кто переигрывает шаг, и другого
    момента у него нет: шаг начинается ровно там, где оборвался прошлый.

    Уборка идёт ДО тела шага, а не после: артефакт, который шаг перезапишет
    сам, уборке не нужен, а вот огрызок, оставшийся от попытки, обязан
    исчезнуть даже если эта попытка до своего артефакта не дойдёт вовсе.

    Область — только шаблоны `TEMP_ARTIFACT_PATTERNS` и только файлы: имя,
    не совпавшее с шаблоном, это чей-то артефакт, и снести его молча
    означало бы вычистить историю раунда вместо мусора. Финализированный
    раунд убирается наравне с прочими — маркер I3 закрывает от правок
    артефакты, а не следы недописанного файла, и хранить их в закрытом
    раунде вечно было бы худшим из прочтений [REQ-016].

    Отсутствие директории — законный вход, и обрабатывается оно не проверкой,
    а `Path.glob`, который по несуществующему каталогу не даёт ничего: раунд
    появляется на диске вместе с первым своим артефактом, и до него убирать
    попросту нечего. Отбор файлов тоже живёт в выражении, а не в ветке —
    уборка обязана быть тотальной: у шага VERIFYING нет и не должно быть
    формы, способной отменить его собственный переход ([REQ-004]).
    """
    directory = round_dir(artifact_root, round_no)
    for pattern in TEMP_ARTIFACT_PATTERNS:
        leftovers = sorted(path for path in directory.glob(pattern) if path.is_file())
        for leftover in leftovers:
            leftover.unlink(missing_ok=True)


def _deciding_inputs(ctx: StepContext) -> DecidingInputs:
    """Снимок раунда N для ядра — четыре источника, ни одного вывода.

    Ревью и отчёт берутся у раунда N, замечания и патч — у соседей по
    истории, и каждый из них назван отдельной функцией `history`: единая
    «загрузи всё» скрыла бы подмену раунда там, где она дороже всего.

    `patch_current` — строка даже когда файла нет ([REQ-013]): раунд без
    правок законен, и падать на нём шагу не на чем. `patch_two_back`
    отсутствие сохраняет как `None` — для ядра «правок не было» и
    «сравнивать не с чем» это разные входы.
    """
    artifacts = ctx.artifact_root
    round_no = ctx.round
    state = ctx.fsm.state
    return DecidingInputs(
        round=round_no,
        mode=state.task.mode,
        review=round_review(artifacts, round_no),
        verification=_round_verification(artifacts, round_no),
        carried_issues=carried_issues(artifacts, round_no - 1),
        patch_current=load_patch(artifacts, round_no) or "",
        patch_two_back=load_patch(artifacts, round_no - 2),
        issue_history=issue_history(artifacts, round_no),
        budget_used=state.budget_used,
        limits=state.limits,
    )


def _write_decision(artifact_root: Path, round_no: int, decision: Decision) -> None:
    """Пишет `decision.json`; уже финализированный раунд — не ошибка шага.

    Маркер I3 ставит сам шаг, и ставит его ДО перехода — значит между ним и
    переходом есть окно (`commit_round`, обрыв процесса), после которого
    resume поднимает сессию всё ещё в `DECIDING`, а раунд уже закрыт от
    записи. Повтор шага упёрся бы в собственный маркер, и упирался бы в
    него каждой следующей попыткой: фаза, из которой нет выхода. REQ-015
    требует ровно обратного — повтор прерванного шага обязан дойти до
    конца, и `commit_round` для этого сделан идемпотентным ([DESIGN-011]).

    Терпимость держится на детерминизме: входы шага собраны с диска и из
    сохранённого состояния, поэтому повтор выносит то же решение, что уже
    лежит в раунде. Разойдись они — переписывать финализированный артефакт
    шаг не вправе (I3, [REQ-016]) и молчать тоже: переход ушёл бы в фазу, о
    которой `decision.json` рассказывает другое. Поэтому исходная ошибка
    уходит наружу как есть.
    """
    try:
        write_round_artifact(
            artifact_root,
            round_no,
            DECISION_NAME,
            decision.model_dump_json(by_alias=True),
        )
    except RoundImmutableError:
        if load_decision(artifact_root, round_no) != decision:
            raise


def _doc_spec(ctx: StepContext) -> DocSessionSpec | None:
    """Описание doc-сессии либо `None` для develop/analyze (SPEC-002 §5.1).

    Развилка идёт по РЕЖИМУ, а не по наличию `documents`: `Mode.DOCUMENT`
    без описания контура — не «сессия попроще», а сборка, при которой
    ревьюер не узнает набора id чеклиста, а `validate_doc_review` не узнает
    контура. Обе половины V1 молча отключились бы, и doc-сессия сошлась бы
    по критерию develop-раунда. Поэтому это `AssertionError`: сюда приводит
    ошибка композиции, а не действие пользователя.
    """
    if ctx.fsm.state.task.mode is not Mode.DOCUMENT:
        return None
    if ctx.documents is None:
        raise AssertionError(
            "сессия объявлена в режиме document, но контур и документы не "
            "переданы: без них ни промпт §5.1/§5.2, ни правила V1–V8 не "
            "собираются — composition root подал doc-сессию как обычную"
        )
    return ctx.documents


def _author_prompt(ctx: StepContext, round_no: int, prior: PriorRound) -> str:
    """Промпт автора: develop/analyze (§6.1) либо doc-раунд (§5.1 SPEC-002).

    Doc-автор получает не артефакты прошлого раунда, а пути документов и
    архитектурные находки, ради которых открыта ревизия (§7.3): источник
    истины для него — файлы рабочей директории, а не пересказ. Директива
    оркестратора приходит из решения прошлого раунда — тем же каналом, что и
    у develop-автора, и другого у неё нет.
    """
    spec = _doc_spec(ctx)
    if spec is None:
        return build_author_prompt(
            task=ctx.fsm.state.task,
            round=round_no,
            prior_review=prior.review,
            prior_verification=prior.verification,
            prior_decision=prior.decision,
        )
    return build_doc_author_prompt(
        contour=spec.contour,
        task_text=ctx.fsm.state.task.prompt,
        doc_paths=spec.doc_paths,
        directive=None
        if prior.decision is None
        else prior.decision.next_round_directive,
        adopted_findings=load_adopted_findings(ctx.artifact_root),
    )


def _reviewer_prompt(
    ctx: StepContext,
    round_no: int,
    prior: PriorRound,
    verification: VerificationReport,
) -> str:
    """Промпт ревьюера: develop/analyze (§6.2) либо doc-раунд (§5.2 SPEC-002).

    Doc-ревьюер получает ТЕКСТЫ документов, а не пути: doc-ревью охватывает
    несколько документов сразу, и вставлены они внутрь меток «данные, не
    инструкции» той же механикой, что текст автора у develop-ревьюера.
    """
    spec = _doc_spec(ctx)
    if spec is None:
        return build_reviewer_prompt(
            task=ctx.fsm.state.task,
            round=round_no,
            proposal_path=_relative_artifact(ctx, round_no, PROPOSAL_NAME),
            patch_path=_relative_artifact(ctx, round_no, CHANGES_PATCH_NAME),
            verification=verification,
            prior_review=prior.review,
            prior_decision=prior.decision,
        )
    return build_doc_reviewer_prompt(
        contour=spec.contour,
        doc_texts=_doc_texts(ctx, spec),
        verification=verification,
        checklist=spec.checklist,
    )


def _doc_texts(ctx: StepContext, spec: DocSessionSpec) -> Mapping[str, str]:
    """Тексты документов контура; отсутствующий файл в промпт не попадает.

    Отсутствие законно и постоянно: в spec-r1 спеки ещё нет, в pair-r1 может
    не быть плана — их и пишет автор. Пустая строка вместо содержимого
    сказала бы ревьюеру «документ пуст», а это другой факт, и вердикт по нему
    был бы другим.
    """
    texts: dict[str, str] = {}
    for relative in spec.doc_paths:
        path = ctx.workspace_root / relative
        if path.is_file():
            texts[relative] = path.read_text(encoding="utf-8", errors="replace")
    return texts


def _accepted_review(
    ctx: StepContext, text: str, verification: VerificationReport, round_no: int
) -> Review:
    """Текст ревьюера → принятая §4.4 модель; иначе ошибка для повтора.

    Три исхода отказа — нет JSON, не та схема, не приняли правила §4.4 —
    поднимаются как исключения, потому что для schema-retry ([DESIGN-006])
    это один и тот же факт: вывод агента не той формы, и лечится он
    повтором с текстом ошибки, а не ветвлением здесь.

    В `Mode.DOCUMENT` к §4.4 добавляются правила V1–V5, V7–V8 (§5.2
    SPEC-002), и **порядок вызова фиксирован**: `validate_doc_review`
    получает ревью ДО `degrade_unevidenced_issues`, то есть до того, как
    §4.4 понизит безевиденсный blocker до `minor`. Иначе `approve` с
    голословным блокером, `S1: pass` и без `defect_class` прошёл бы V5/V7/V8
    — к моменту их проверки блокера в модели уже не было бы. Отсюда и два
    отдельных вызова вместо одного конвейера: `validate_review` возвращает
    деградированную копию, и подать её в doc-правила значило бы проверить
    не то ревью, которое прислал агент.

    Причины обоих слоёв складываются в ОДИН список: для schema-retry это одна
    неудачная попытка, и разделить её на две значило бы дать агенту чинить
    половину нарушений за раз, тратя лимит повторов на то же ревью.

    Возвращается `acceptance.review` — деградированная копия: исходная
    модель сохранила бы `blocker`, который §4.4 уже не признал, и следующий
    раунд читал бы его как настоящий.
    """
    parsed = Review.model_validate_json(extract_json_object(text))
    spec = _doc_spec(ctx)
    doc_reasons = (
        ()
        if spec is None
        else validate_doc_review(
            parsed,
            contour=spec.contour,
            checklist=spec.checklist,
            verification=verification,
        )
    )
    acceptance = validate_review(parsed, verification)
    reasons = [*acceptance.rejection_reasons, *doc_reasons]
    if reasons:
        raise ReviewNotAccepted(reasons, round_no=round_no)
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


def round_review(artifact_root: Path, round_no: int) -> Review:
    """Ревью раунда `round_no`; его отсутствие — ошибка порядка.

    `AssertionError` по той же причине, что и у отчёта проверок: войти в
    DECIDING раньше, чем REVIEWING положил ревью на диск, write-ahead-
    переход не даёт. Значит пустое место здесь означает сломанную
    диспетчеризацию цикла, а не действие пользователя.

    Публичная, потому что читателя двое: снимок `DECIDING` и опрос
    `RoundBoundaryPolicy` на границе раунда (`runtime/loop.py`, SPEC-002
    §7.1). Оба обязаны видеть ОДНО ревью — второй читатель с собственным
    разбором артефакта разошёлся бы с ядром ровно тогда, когда политика
    паркует сессию по находке, которой решение не видело.
    """
    review_model = load_review(artifact_root, round_no)
    if review_model is None:
        raise AssertionError(
            f"нет review.json раунда {round_no:03d}: шаг DECIDING вызван до "
            "REVIEWING — решать раунд, по которому ревьюер не высказался, "
            "не из чего"
        )
    return review_model


def _round_verification(artifact_root: Path, round_no: int) -> VerificationReport:
    """Отчёт проверок раунда `round_no`; его отсутствие — ошибка порядка.

    `AssertionError`, а не доменная ошибка: `VERIFYING` всегда
    предшествует `REVIEWING`, и write-ahead-переход не даёт войти сюда
    раньше, чем отчёт лёг на диск. Значит пустое место здесь означает
    сломанную диспетчеризацию цикла, а не действие пользователя.
    """
    report = load_verification(artifact_root, round_no)
    if report is None:
        raise AssertionError(
            f"нет verification.json раунда {round_no:03d}: шаг REVIEWING "
            "вызван до VERIFYING — ревьюер не может судить по гейтам, "
            "которых не прогоняли"
        )
    return report


def _relative_artifact(ctx: StepContext, round_no: int, name: str) -> str:
    """Путь артефакта раунда относительно рабочего корня, POSIX-разделителями.

    Единственное место шага, где встречаются оба корня (SPEC-002 §4.1):
    артефакт лежит под `artifact_root`, а читает его ревьюер, запущенный из
    `workspace_root`, — значит и назван он должен быть от рабочего корня.

    Относительный — не косметика: абсолютный путь машины оркестратора
    бесполезен ревьюеру и заодно утёк бы в промпт раскладкой файловой
    системы. `as_posix` фиксирует разделитель: промпт обязан быть одинаковым
    на любой ОС (NFR-002).

    Отсюда предусловие разведённых корней: `artifact_root` обязан лежать
    ВНУТРИ `workspace_root` — так его и размещает §4.1
    (`pipelines/<slug>/sessions/<revision>/`). Держит его не `relative_to`
    здесь, а `composition._normalized_roots`: он отвергает журнал снаружи
    репозитория на СБОРКЕ и там же приводит оба корня к одной форме. Сюда
    приходят уже нормализованные пути, поэтому `relative_to` отвечает по
    расположению, а не по тому, в какой форме корни подали вызывающему.
    Проверка на сборке, а не тут, потому что этот шаг идёт после
    `reset --hard`, работы автора и прогона гейтов: отказ на этой строке
    стоил бы полного раунда.
    """
    artifact = round_artifact(ctx.artifact_root, round_no, name)
    return artifact.relative_to(ctx.workspace_root).as_posix()


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
