"""Отбор, сортировка и рендер секций промпта (§6.1–6.2, [DESIGN-003]).

Здесь живут ВСЕ правила «что попадает в промпт и в каком порядке»:
`author.py` и `reviewer.py` остаются тонкими композиторами и переиспользуют
эти функции, вместо того чтобы дублировать логику отбора у себя.

Два инварианта модуля:

1. Пустая секция не рендерится вообще ([ADR-003]): `render_*` на пустом
   входе возвращает `""` — ни заголовка, ни строки «(нет)». Заголовок над
   пустотой агент читает как факт («issues проверены, их нет»), тогда как
   на деле это лишь отсутствие данных.
2. Артефактный текст оборачивается `wrap_artifact_data` на своём уровне
   гранулярности — один блок на issue и на гейт, а не один на весь промпт.
   Заголовки секций и вычисленные оркестратором сводки (`overall`,
   `diff_stats`) остаются СНАРУЖИ меток: внутри — только то, что пришло от
   агента или инструмента (§6.3).

Модуль чист: ни I/O, ни времени, ни случайности — результат зависит только
от аргументов (NFR-001, NFR-002).
"""

from collections.abc import Iterable, Sequence
from typing import Final

from disputatio.context.tags import wrap_artifact_data
from disputatio.contracts.review import Issue, Severity
from disputatio.contracts.verification import (
    GateResult,
    GateStatus,
    VerificationReport,
)

OPEN_ISSUES_TITLE: Final = "## Открытые замечания ревьюера прошлого раунда"
RESOLVED_ISSUES_TITLE: Final = "## Замечания, заявленные автором как решённые"
FAILED_GATES_TITLE: Final = "## Проваленные детерминированные проверки"
DIRECTIVE_TITLE: Final = "## Директива оркестратора на этот раунд"
VERIFICATION_TITLE: Final = "## Отчёт детерминированных проверок"

_SEVERITY_ORDER: Final = (
    Severity.BLOCKER,
    Severity.MAJOR,
    Severity.MINOR,
    Severity.NIT,
)
# Ключи — строковые значения: `Severity` это `StrEnum`, он хэшируется и
# сравнивается как `str`, поэтому по такому словарю одинаково находятся и
# член enum, и сырая строка из `model_copy(update=...)`.
_SEVERITY_RANK: Final = {
    severity.value: rank for rank, severity in enumerate(_SEVERITY_ORDER)
}
_UNKNOWN_SEVERITY_RANK: Final = len(_SEVERITY_ORDER)


def select_open_issues(
    issues: Iterable[Issue], open_issue_ids: Iterable[str]
) -> list[Issue]:
    """Issues, чей `id` есть в `open_issue_ids`, по убыванию серьёзности.

    Порядок — `blocker → major → minor → nit`; внутри одной severity
    сохраняется исходный порядок ревьюера (сортировка стабильна), чтобы
    промпт не переставлял замечания между раундами без причины.
    """
    carried = set(open_issue_ids)
    return _sorted_by_severity(issue for issue in issues if issue.id in carried)


def select_resolved_issues(
    issues: Iterable[Issue], open_issue_ids: Iterable[str]
) -> list[Issue]:
    """Дополнение `select_open_issues` — issues, ушедшие из открытых.

    Для ревьюера это список «автор заявил решённым»: он обязан проверить,
    действительно ли исправлено (§6.2). Порядок — тот же, что у открытых.
    """
    carried = set(open_issue_ids)
    return _sorted_by_severity(issue for issue in issues if issue.id not in carried)


def select_failed_gates(gates: Iterable[GateResult]) -> list[GateResult]:
    """Только гейты со статусом `fail`; `pass` и `skip` отсеиваются.

    Сравнение значением, а не идентичностью: `model_copy(update=...)`
    обходит валидацию и оставляет в поле сырую строку — проверка на `is`
    тихо потеряла бы такой гейт и отдала бы автору неполный список.
    """
    return [gate for gate in gates if gate.status == GateStatus.FAIL]


def render_issues_section(issues: Sequence[Issue], *, title: str) -> str:
    """Секция с замечаниями; `title` задаёт вызывающий (открытые/решённые).

    Каждый issue — отдельный artifact-блок: все его поля пришли от
    ревьюера, поэтому внутрь меток попадают и `claim`/`evidence`, и `id`
    с `file`. Снаружи остаётся только заголовок.
    """
    if not issues:
        return ""
    blocks = [wrap_artifact_data(_render_issue(issue)) for issue in issues]
    return "\n".join([title, *blocks])


def render_failed_gates_section(gates: Sequence[GateResult]) -> str:
    """Секция проваленных гейтов: имя, команда и `tail` вывода.

    Фильтрация внутри — вызывающему достаточно передать `verification.gates`.
    Полного stdout здесь нет и быть не может: обрезкой вывода занимается
    verifier, а в артефакте живёт только `tail`.
    """
    failed = select_failed_gates(gates)
    if not failed:
        return ""
    blocks = [wrap_artifact_data(_render_gate(gate)) for gate in failed]
    return "\n".join([FAILED_GATES_TITLE, *blocks])


def render_directive_section(directive: str | None) -> str:
    """Секция с `decision.next_round_directive`; `None`/пробелы → `""`.

    Директива — текст оркестратора, но она цитирует ревьюера, поэтому
    попадает внутрь меток наравне с артефактами.
    """
    if directive is None or not directive.strip():
        return ""
    return f"{DIRECTIVE_TITLE}\n{wrap_artifact_data(directive)}"


def render_verification_section(verification: VerificationReport | None) -> str:
    """Полный отчёт проверок раунда: сводка и ВСЕ гейты (§6.2).

    В отличие от `render_failed_gates_section`, ничего не фильтрует:
    ревьюер должен видеть и `pass`, и `skip` с его `reason` — иначе он не
    отличит «проверено и прошло» от «не запускалось». `overall` и
    `diff_stats` вычислены оркестратором и стоят вне меток.
    """
    if verification is None:
        return ""
    stats = verification.diff_stats
    parts = [
        VERIFICATION_TITLE,
        f"overall: {verification.overall}",
        (
            f"diff_stats: files={stats.files}, insertions={stats.insertions}, "
            f"deletions={stats.deletions}"
        ),
    ]
    parts.extend(wrap_artifact_data(_render_gate(gate)) for gate in verification.gates)
    return "\n".join(parts)


def _sorted_by_severity(issues: Iterable[Issue]) -> list[Issue]:
    """Стабильная сортировка по severity — единственный ключ сортировки."""
    return sorted(issues, key=_severity_rank)


def _severity_rank(issue: Issue) -> int:
    """Ранг severity; незнакомое значение уезжает в конец, а не падает."""
    return _SEVERITY_RANK.get(issue.severity, _UNKNOWN_SEVERITY_RANK)


def _render_issue(issue: Issue) -> str:
    """Поля одного issue построчно; пустые опциональные поля опускаются."""
    lines = [
        f"id: {issue.id}",
        f"severity: {issue.severity}",
        f"file: {issue.file}",
    ]
    if issue.line_hint is not None:
        lines.append(f"line_hint: {issue.line_hint}")
    lines.append(f"claim: {issue.claim}")
    if issue.evidence:
        lines.append(f"evidence: {issue.evidence}")
    if issue.suggestion is not None:
        lines.append(f"suggestion: {issue.suggestion}")
    return "\n".join(lines)


def _render_gate(gate: GateResult) -> str:
    """Поля одного гейта; `tail` — последним, он единственный многострочный."""
    lines = [
        f"gate: {gate.name}",
        f"cmd: {gate.cmd}",
        f"status: {gate.status}",
    ]
    if gate.exit_code is not None:
        lines.append(f"exit_code: {gate.exit_code}")
    if gate.reason is not None:
        lines.append(f"reason: {gate.reason}")
    if gate.tail:
        lines.append(f"tail:\n{gate.tail}")
    return "\n".join(lines)
