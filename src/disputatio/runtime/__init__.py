"""disputatio.runtime: композиция портов, оркестраторный цикл, CLI.

Публичное API workstream'а: composition root ([REQ-001], [DESIGN-001]),
конфиг сессии ([DESIGN-014]), порт git-операций (ADR-005), иерархия доменных
ошибок ([DESIGN-020]) и машинная проверка чистоты ядра INV-11 ([REQ-002],
[DESIGN-002]).

Внутренние модули публичным входом не являются: связывание портов с
реализациями живёт в `composition.py` и импортируется отсюда, а не по пути
подмодуля.
"""

from disputatio.runtime.composition import (
    ADAPTER_FACTORIES,
    AdapterFactory,
    RuntimeDeps,
    build_runtime,
)
from disputatio.runtime.config import AgentConfig, LimitsConfig, RuntimeConfig
from disputatio.runtime.errors import (
    ConfigError,
    DirtyWorkingTree,
    DisputatioError,
    NotAGitRepository,
    ReviewParseError,
    SessionNotFound,
    UnknownAdapterError,
)
from disputatio.runtime.git import GitOps
from disputatio.runtime.purity import (
    FORBIDDEN_ROOTS,
    PurityViolation,
    scan_package_purity,
)

__all__ = [
    "ADAPTER_FACTORIES",
    "FORBIDDEN_ROOTS",
    "AdapterFactory",
    "AgentConfig",
    "ConfigError",
    "DirtyWorkingTree",
    "DisputatioError",
    "GitOps",
    "LimitsConfig",
    "NotAGitRepository",
    "PurityViolation",
    "ReviewParseError",
    "RuntimeConfig",
    "RuntimeDeps",
    "SessionNotFound",
    "UnknownAdapterError",
    "build_runtime",
    "scan_package_purity",
]
