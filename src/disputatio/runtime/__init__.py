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
from disputatio.runtime.config import (
    AgentConfig,
    LimitsConfig,
    RuntimeConfig,
    load_config,
    load_config_file,
)
from disputatio.runtime.errors import (
    AdoptionScopeError,
    BaseRevisionNotFound,
    ConfigError,
    ControlPlaneTampered,
    DirtyWorkingTree,
    DisputatioError,
    EmptyRepository,
    ExternalEditError,
    GitCommandError,
    NotAGitRepository,
    PipelineAlreadyExists,
    PipelineNotResumable,
    ProtectedBranchError,
    ReviewNotAccepted,
    ReviewParseError,
    SessionNotFound,
    UnknownAdapterError,
    UnprovableSemantics,
)
from disputatio.runtime.git import (
    ROUND_COMMIT_PATTERN,
    ROUND_COMMIT_TEMPLATE,
    GitCli,
    GitOps,
    StatusEntry,
    base_rev,
    preflight,
)
from disputatio.runtime.pipeline_config import (
    PipelineConfig,
    check_run_preconditions,
    load_pipeline_config,
    validate_anchor_path,
)
from disputatio.runtime.purity import (
    FORBIDDEN_ROOTS,
    PurityViolation,
    scan_package_purity,
)

__all__ = [
    "ADAPTER_FACTORIES",
    "FORBIDDEN_ROOTS",
    "ROUND_COMMIT_PATTERN",
    "ROUND_COMMIT_TEMPLATE",
    "AdapterFactory",
    "AdoptionScopeError",
    "AgentConfig",
    "BaseRevisionNotFound",
    "ConfigError",
    "ControlPlaneTampered",
    "DirtyWorkingTree",
    "DisputatioError",
    "EmptyRepository",
    "ExternalEditError",
    "GitCli",
    "GitCommandError",
    "GitOps",
    "LimitsConfig",
    "NotAGitRepository",
    "PipelineAlreadyExists",
    "PipelineConfig",
    "PipelineNotResumable",
    "ProtectedBranchError",
    "PurityViolation",
    "ReviewNotAccepted",
    "ReviewParseError",
    "RuntimeConfig",
    "RuntimeDeps",
    "SessionNotFound",
    "StatusEntry",
    "UnknownAdapterError",
    "UnprovableSemantics",
    "base_rev",
    "build_runtime",
    "check_run_preconditions",
    "load_config",
    "load_config_file",
    "load_pipeline_config",
    "preflight",
    "scan_package_purity",
    "validate_anchor_path",
]
