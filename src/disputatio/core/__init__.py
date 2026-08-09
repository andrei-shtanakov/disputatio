"""Ядро FSM сессии: граф переходов, писатели, DECIDING (workspace w-fsm).

Публичный API волны 2 ([DESIGN-010]): потребители (w-runtime) импортируют
FSM, enum'ы, чистые функции и константы причин только отсюда, не из
подмодулей (`disputatio.core.transitions`, `disputatio.core.writers`, …).
"""

from disputatio.core.deciding import (
    REASON_ANTI_SYCOPHANCY,
    REASON_BUDGET_TOKENS,
    REASON_BUDGET_WALL,
    REASON_CONTINUE,
    REASON_CONVERGED,
    REASON_MAX_ROUNDS,
    REASON_OSCILLATION_DIFF,
    REASON_OSCILLATION_ISSUE,
    DecidingInputs,
    DecisionDraft,
    decide,
)
from disputatio.core.machine import RetryAction, SessionFsm, is_partial
from disputatio.core.oscillation import (
    CLAIM_SIMILARITY_THRESHOLD,
    OSCILLATION_DIFF_THRESHOLD,
    find_repeated_issue,
    patch_similarity,
)
from disputatio.core.transitions import (
    TERMINAL_PHASES,
    TRANSITIONS,
    InvalidTransition,
    check_transition,
)
from disputatio.core.writers import ACTIVE_WRITER, Writer, active_writer

__all__ = [  # noqa: RUF022 — отсортирован по codepoint (тест), не isort-групп
    "ACTIVE_WRITER",
    "CLAIM_SIMILARITY_THRESHOLD",
    "DecidingInputs",
    "DecisionDraft",
    "InvalidTransition",
    "OSCILLATION_DIFF_THRESHOLD",
    "REASON_ANTI_SYCOPHANCY",
    "REASON_BUDGET_TOKENS",
    "REASON_BUDGET_WALL",
    "REASON_CONTINUE",
    "REASON_CONVERGED",
    "REASON_MAX_ROUNDS",
    "REASON_OSCILLATION_DIFF",
    "REASON_OSCILLATION_ISSUE",
    "RetryAction",
    "SessionFsm",
    "TERMINAL_PHASES",
    "TRANSITIONS",
    "Writer",
    "active_writer",
    "check_transition",
    "decide",
    "find_repeated_issue",
    "is_partial",
    "patch_similarity",
]
