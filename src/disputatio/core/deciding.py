"""Снимок входов `DECIDING`, критерий converged и `decide()` §5 (DESIGN-004/005).

`DecidingInputs`/`DecisionDraft` — frozen-датаклассы: значения, а не объекты
с поведением. `is_converged()` реализует критерий сходимости [REQ-007] и
анти-сикофантию [REQ-008] в одном месте — по DESIGN-005 защита раунда 1
живёт внутри критерия converged, не как отдельная ветка. `decide()`
реализует строгий top-down порядок §5 [REQ-006]: converged → budget_hit
[REQ-010] → indeterminate (§5.2a) → осцилляция [REQ-011] → max_rounds
[REQ-012] → иначе continue, линейной цепочкой ранних `return` без таблиц
приоритетов; прогноз бюджета (§5.2) проверяется последним и заменяет только
исход, который иначе был бы `CONTINUE`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from statistics import median
from typing import Final

from disputatio.contracts import (
    BudgetSnapshot,
    BudgetUsed,
    Issue,
    Limits,
    Mode,
    Outcome,
    OverallStatus,
    Review,
    Severity,
    Verdict,
    VerificationReport,
)
from disputatio.core.oscillation import (
    OSCILLATION_DIFF_THRESHOLD,
    find_repeated_issue,
    patch_similarity,
)

REASON_CONVERGED: Final = "approve_with_gates_pass"
REASON_ANTI_SYCOPHANCY: Final = "anti_sycophancy_forced_review"
REASON_BUDGET_TOKENS: Final = "budget_hit: tokens"
REASON_BUDGET_WALL: Final = "budget_hit: wall_seconds"
REASON_BUDGET_PREDICTED: Final = "budget_hit: predicted"
REASON_OSCILLATION_DIFF: Final = "oscillation: diff-similarity"
REASON_OSCILLATION_ISSUE: Final = "oscillation: repeated issue"
REASON_MAX_ROUNDS: Final = "max_rounds"
REASON_VERIFICATION_INDETERMINATE: Final = "verification_indeterminate"
REASON_CONTINUE: Final = "continue_revise_cycle"

_OPEN_SEVERITIES: Final = frozenset({Severity.BLOCKER, Severity.MAJOR})
"""Severity, при которой замечание объявляется открытым (порог §4.4)."""

_ANTI_SYCOPHANCY_DIRECTIVE: Final = (
    "Раунд 1 принят только для analyze без правок кода; требуется один "
    "содержательный цикл ревью: минимум 3 замечания любой severity либо "
    "явное обоснование в checked, почему их нет."
)


@dataclass(frozen=True, slots=True)
class DecidingInputs:
    """Снимок раунда N, собранный w-runtime из артефактов (DESIGN-004).

    `budget_snapshots` — `budget_snapshot` решений прошлых раундов, номер
    раунда → снимок (§4.5). Раунда без снимка (решение прежней версии) в
    отображении нет: отсутствие нулём не заменяется (§5.2). Снимок
    текущего раунда — сам `budget_used`, расход на входе в `DECIDING`.
    """

    round: int
    mode: Mode
    review: Review
    verification: VerificationReport
    carried_issues: tuple[Issue, ...]
    patch_current: str
    patch_two_back: str | None
    issue_history: Mapping[int, tuple[Issue, ...]]
    budget_used: BudgetUsed
    limits: Limits
    budget_snapshots: Mapping[int, BudgetSnapshot] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecisionDraft:
    """Исход `decide()` — чистое значение, до материализации `Decision`.

    `budget_snapshot` — `budget_used` на входе в `DECIDING` (§4.5); `None`
    только у черновика, восстановленного из решения прежней версии.
    """

    outcome: Outcome
    reason: str
    open_issues_carried: tuple[str, ...]
    next_round_directive: str | None
    forced_review: bool
    budget_snapshot: BudgetSnapshot | None = None


def _analyze_without_gates(inputs: DecidingInputs) -> bool:
    """Карман §5.1 п.2: `analyze` с пустым набором гейтов.

    «Пустой» — в отчёте нет ни одного гейта; набор из одних `skip` карманом
    не является. Одно определение на двух читателей — `_gates_pass` и
    условие §5.2a, — чтобы карман не разъехался между сходимостью и
    остановкой.
    """
    return inputs.mode is Mode.ANALYZE and not inputs.verification.gates


def _gates_pass(inputs: DecidingInputs) -> bool:
    """`overall == pass`, либо `analyze` без гейтов (§5.1)."""
    if inputs.verification.overall is OverallStatus.PASS:
        return True
    return _analyze_without_gates(inputs)


def _no_evidence(inputs: DecidingInputs) -> bool:
    """Раунд без свидетельства вне кармана §5.1 п.2 (§5.2a).

    Такой раунд не сходится ни при каком поведении агентов, поэтому сессия
    встаёт на нём, а не на исходе `max_rounds`, — с причиной, которая
    называет саму беду, а не её симптом.
    """
    if inputs.verification.overall is not OverallStatus.INDETERMINATE:
        return False
    return not _analyze_without_gates(inputs)


def _no_carried_blocker(inputs: DecidingInputs) -> bool:
    """В `carried_issues` нет ни одного `blocker` (§5.1)."""
    return not any(
        issue.severity is Severity.BLOCKER for issue in inputs.carried_issues
    )


def _passes_anti_sycophancy(inputs: DecidingInputs) -> bool:
    """Раунд 1: approve засчитывается только `analyze` без правок кода [REQ-008]."""
    if inputs.round != 1:
        return True
    return inputs.mode is Mode.ANALYZE and not inputs.patch_current.strip()


def _converged_except_anti_sycophancy(inputs: DecidingInputs) -> bool:
    """approve+gates_pass+no_carried_blocker, без учёта анти-сикофантии (§5.1)."""
    if inputs.review.verdict is not Verdict.APPROVE:
        return False
    if not _gates_pass(inputs):
        return False
    return _no_carried_blocker(inputs)


def is_converged(inputs: DecidingInputs) -> bool:
    """Критерий CONVERGED §5.1, включая анти-сикофантию раунда 1 [REQ-007/008]."""
    return _converged_except_anti_sycophancy(inputs) and _passes_anti_sycophancy(inputs)


def _anti_sycophancy_blocked(inputs: DecidingInputs) -> bool:
    """True, если converged не сработал именно из-за анти-сикофантии раунда 1."""
    return _converged_except_anti_sycophancy(inputs) and not _passes_anti_sycophancy(
        inputs
    )


def _budget_hit_reason(inputs: DecidingInputs) -> str | None:
    """`REASON_BUDGET_TOKENS`/`REASON_BUDGET_WALL` при строгом превышении
    лимита [REQ-010].
    """
    if inputs.budget_used.tokens > inputs.limits.max_total_tokens:
        return REASON_BUDGET_TOKENS
    if inputs.budget_used.wall_seconds > inputs.limits.max_wall_seconds:
        return REASON_BUDGET_WALL
    return None


def budget_snapshot(used: BudgetUsed) -> BudgetSnapshot:
    """Снимок §4.5 из накопленного расхода: три поля, без стоимости."""
    return BudgetSnapshot(
        tokens=used.tokens,
        wall_seconds=used.wall_seconds,
        unreported_turns=used.unreported_turns,
    )


def _round_snapshots(inputs: DecidingInputs) -> list[BudgetSnapshot | None]:
    """Снимки раундов 0…N; `None` — снимка нет.

    Раунд 0 — начальный расход сессии: ноль по построению, но только при
    `tracked_from_start` (§4.1), прежний учёт базы не даёт. Раунд N —
    текущий `budget_used`.
    """
    zero = (
        BudgetSnapshot(tokens=0, wall_seconds=0.0, unreported_turns=0)
        if inputs.budget_used.tracked_from_start
        else None
    )
    prior = [inputs.budget_snapshots.get(k) for k in range(1, inputs.round)]
    return [zero, *prior, budget_snapshot(inputs.budget_used)]


def _cost_observations(inputs: DecidingInputs) -> tuple[list[int], list[float]]:
    """Пригодные наблюдения стоимости раундов — (токены, секунды) §5.2.

    Интервал пригоден, только если оба соседних снимка существуют: через
    пропуск разность не берётся. Токенный — ещё и только если
    `unreported_turns` за раунд не вырос.
    """
    tokens: list[int] = []
    wall: list[float] = []
    for before, after in pairwise(_round_snapshots(inputs)):
        if before is None or after is None:
            continue
        wall.append(after.wall_seconds - before.wall_seconds)
        if after.unreported_turns <= before.unreported_turns:
            tokens.append(after.tokens - before.tokens)
    return tokens, wall


def _budget_predicted(inputs: DecidingInputs) -> bool:
    """Остаток меньше медианы пригодных наблюдений хоть по одному ресурсу.

    Ресурсы раздельны; без наблюдений по ресурсу прогноз по нему не
    применяется (§5.2). Лимиты заданы всегда — «без лимита» нет.
    """
    tokens, wall = _cost_observations(inputs)
    used = inputs.budget_used
    limits = inputs.limits
    if tokens and limits.max_total_tokens - used.tokens < median(tokens):
        return True
    return bool(wall) and limits.max_wall_seconds - used.wall_seconds < median(wall)


def _oscillation_reason(inputs: DecidingInputs) -> str | None:
    """Diff-similarity, затем repeated-issue, в этом порядке (DESIGN-006) [REQ-011].

    Diff-similarity пропускается для `analyze`: в этом режиме `patch_current`
    всегда пуст, а `patch_similarity("", "") == 1.0` (два пустых патча
    считаются идентичными по определению) ложно триггернуло бы осцилляцию
    на каждой analyze-сессии, дошедшей до раунда 3, хотя правки кода там в
    принципе не производятся — `_gates_pass` для `analyze` делает такое же
    исключение.
    """
    if inputs.mode is not Mode.ANALYZE and inputs.patch_two_back is not None:
        similarity = patch_similarity(inputs.patch_current, inputs.patch_two_back)
        if similarity > OSCILLATION_DIFF_THRESHOLD:
            return REASON_OSCILLATION_DIFF
    repeated = find_repeated_issue(inputs.review.issues, inputs.issue_history)
    if repeated is not None:
        return f"{REASON_OSCILLATION_ISSUE}: {repeated.file}/{repeated.id}"
    return None


def _build_directive(review: Review) -> str:
    """Директива автору следующего раунда, собранная из issues ревью (§5)."""
    if not review.issues:
        return "Продолжить работу над задачей: цикл ревью продолжается."
    return "; ".join(f"{issue.file}: {issue.claim}" for issue in review.issues)


def decide(inputs: DecidingInputs) -> DecisionDraft:
    """`DECIDING` §5: converged → budget_hit → indeterminate → осцилляция →
    max_rounds → прогноз бюджета → continue.

    Строгий top-down порядок [REQ-006] — линейная цепочка ранних `return`,
    первое сработавшее условие терминально. Раунд без свидетельства (§5.2a)
    стоит после бюджета и до осцилляции: исчерпанный бюджет — более сильная
    причина, а осцилляция и `max_rounds` назвали бы симптом. Раньше
    принудительного цикла анти-сикофантии он тоже стоит: тот терминальных
    условий не отменяет (§5.1).

    `open_issues_carried` перечисляет открытое ПОСЛЕ раунда N, а не до него:
    §4.5 показывает у решения раунда 3 идентификатор `R3-2` — id его
    собственного ревью, — и оба читателя поля ждут того же (манифест §3.2
    пересекает список с ревью раунда-источника, снимок следующего `DECIDING`
    — с ревью того же раунда). Один вход сюда переписан бы в выход: возьмись
    список целиком у `inputs.carried_issues`, поле сошлось бы в неподвижную
    точку — раунд 1 приходит без прошлого, значит пусто, и дальше пусто на
    каждом раунде, то есть «открытых замечаний не осталось» у сессии, которая
    как раз из-за них и не сошлась.

    Поэтому список — пришедшее открытым ПЛЮС существенное, названное ревью
    этого раунда. Порог существенности — `blocker|major` §4.4: ровно он
    заставляет ревьюера просить правок, и ровно до `minor` §4.4 деградирует
    голословное замечание, чтобы оно не крутило цикл. Свежий `nit` не
    объявляется открытым по той же причине: замечание, которого никто не
    обязан закрывать, не должно попадать ни в манифест, ни в промпт автора.
    Порядок — пришедшие первыми, в порядке прошлого решения: он уходит в
    следующий снимок, и перестановка сделала бы историю раундов
    невоспроизводимой.

    Пришедшее открытым остаётся открытым, только пока ревью этого раунда его
    ещё называет (disputatio#14). Раньше входящее множество переносилось
    целиком, и сходящаяся сессия объявляла открытым замечание, которого
    ревьюер уже не видит: `decision.json` раунда с `approve` нёс id из
    раунда 1. Наружу это не протекало — оба потребителя пересекают список с
    `issues` ревью, — но `decision.json` читает человек, и в нём было
    написано неверное.

    Прогноз бюджета (§5.2) стоит последним и заменяет только `CONTINUE` —
    обычный и принудительный цикл анти-сикофантии: уже установленную
    причину остановки он не отменяет. Каждый черновик, терминальный тоже,
    несёт `budget_snapshot` — расход на входе в `DECIDING` (§4.5).
    """
    snapshot = budget_snapshot(inputs.budget_used)
    named_now = {issue.id for issue in inputs.review.issues}
    carried_ids = tuple(
        issue.id for issue in inputs.carried_issues if issue.id in named_now
    )
    known = set(carried_ids)
    open_issues_carried = carried_ids + tuple(
        issue.id
        for issue in inputs.review.issues
        if issue.severity in _OPEN_SEVERITIES and issue.id not in known
    )

    if is_converged(inputs):
        return DecisionDraft(
            outcome=Outcome.CONVERGED,
            reason=REASON_CONVERGED,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
            budget_snapshot=snapshot,
        )

    budget_reason = _budget_hit_reason(inputs)
    if budget_reason is not None:
        return DecisionDraft(
            outcome=Outcome.BUDGET_HIT,
            reason=budget_reason,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
            budget_snapshot=snapshot,
        )

    if _no_evidence(inputs):
        return DecisionDraft(
            outcome=Outcome.DEADLOCK,
            reason=REASON_VERIFICATION_INDETERMINATE,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
            budget_snapshot=snapshot,
        )

    oscillation_reason = _oscillation_reason(inputs)
    if oscillation_reason is not None:
        return DecisionDraft(
            outcome=Outcome.DEADLOCK,
            reason=oscillation_reason,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
            budget_snapshot=snapshot,
        )

    if inputs.round >= inputs.limits.max_rounds:
        return DecisionDraft(
            outcome=Outcome.DEADLOCK,
            reason=REASON_MAX_ROUNDS,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
            budget_snapshot=snapshot,
        )

    if _budget_predicted(inputs):
        return DecisionDraft(
            outcome=Outcome.BUDGET_HIT,
            reason=REASON_BUDGET_PREDICTED,
            open_issues_carried=open_issues_carried,
            next_round_directive=None,
            forced_review=False,
            budget_snapshot=snapshot,
        )

    if _anti_sycophancy_blocked(inputs):
        return DecisionDraft(
            outcome=Outcome.CONTINUE,
            reason=REASON_ANTI_SYCOPHANCY,
            open_issues_carried=open_issues_carried,
            next_round_directive=_ANTI_SYCOPHANCY_DIRECTIVE,
            forced_review=True,
            budget_snapshot=snapshot,
        )

    return DecisionDraft(
        outcome=Outcome.CONTINUE,
        reason=REASON_CONTINUE,
        open_issues_carried=open_issues_carried,
        next_round_directive=_build_directive(inputs.review),
        forced_review=False,
        budget_snapshot=snapshot,
    )
