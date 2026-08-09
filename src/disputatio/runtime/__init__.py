"""disputatio.runtime: композиция портов, оркестраторный цикл, CLI.

Публичное API workstream'а. На текущем шаге ([TASK-001]) экспортируется
только AST-скан чистоты ядра — машинная проверка INV-11 ([REQ-002],
[DESIGN-002]); связывание портов с реализациями появится вместе с
composition root.
"""

from disputatio.runtime.purity import (
    FORBIDDEN_ROOTS,
    PurityViolation,
    scan_package_purity,
)

__all__ = [
    "FORBIDDEN_ROOTS",
    "PurityViolation",
    "scan_package_purity",
]
