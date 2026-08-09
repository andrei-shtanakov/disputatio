"""Детектор осцилляции §5.3 — diff-similarity и повторное issue (DESIGN-006).

Две чистые функции + пороги (ADR-W2-02, ADR-W2-03), только `difflib` и
stdlib. `decide()` (TASK-007) вызывает их в строгом порядке: сначала
`patch_similarity` между раундами N и N−2, затем `find_repeated_issue`.
"""

from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Final

from disputatio.contracts.review import Issue

OSCILLATION_DIFF_THRESHOLD: Final = 0.8  # строгое «больше» (§5.3, ADR-W2-02)
CLAIM_SIMILARITY_THRESHOLD: Final = 0.7  # нечёткое совпадение claim (ADR-W2-03)

_MIN_REPEATED_OCCURRENCES: Final = 3  # k >= 3 (issue открыто в 3-й раз)


def _changed_lines(patch: str) -> set[str]:
    """Множество нормализованных изменённых строк патча (ADR-W2-02).

    Строки `+`/`-`, исключая заголовки `+++`/`---`; нормализация — strip
    маркера и хвостовых пробелов.
    """
    changed: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            changed.add(line[1:].rstrip())
    return changed


def patch_similarity(a: str, b: str) -> float:
    """Jaccard-коэффициент над множествами изменённых строк патчей `a`/`b`.

    Два пустых множества (например, два пустых патча подряд) → `1.0`.
    """
    changed_a = _changed_lines(a)
    changed_b = _changed_lines(b)
    if not changed_a and not changed_b:
        return 1.0
    return len(changed_a & changed_b) / len(changed_a | changed_b)


def _normalize_claim(claim: str) -> str:
    """Casefold + схлопывание пробелов для нечёткого сравнения claim."""
    return " ".join(claim.casefold().split())


def _claims_match(a: Issue, b: Issue) -> bool:
    """Тот же `file` и нечётко совпадающий `claim` (ADR-W2-03)."""
    if a.file != b.file:
        return False
    ratio = SequenceMatcher(
        None, _normalize_claim(a.claim), _normalize_claim(b.claim)
    ).ratio()
    return ratio >= CLAIM_SIMILARITY_THRESHOLD


def find_repeated_issue(
    current: Sequence[Issue], history: Mapping[int, Sequence[Issue]]
) -> Issue | None:
    """Issue текущего раунда, открытое в k-й раз (`k >= 3`) — DESIGN-006.

    Совпадение с `history` считается по различным прошлым раундам: issue
    должно нечётко совпасть с issues хотя бы из `k - 1` разных раундов
    (`k >= 3` ⇒ минимум 2 различных прошлых раунда).
    """
    for issue in current:
        matched_rounds = sum(
            1
            for past_issues in history.values()
            if any(_claims_match(issue, past) for past in past_issues)
        )
        if matched_rounds + 1 >= _MIN_REPEATED_OCCURRENCES:
            return issue
    return None
