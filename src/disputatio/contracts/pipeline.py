"""Манифест пайплайна — семейство схемы `disputatio/pipeline/v1` (SPEC-002).

`pipeline.json` — отдельный от раундов debate loop артефакт (§4.2): корень
семейства **не** `ArtifactBase` (`disputatio/v1|v2`) — те две версии
сосуществуют в одном Literal ради default-подстановки на старых вызывающих
местах develop/analyze-сессий (см. `base.py`); у манифеста пайплайна такой
истории нет, и разделяющие `ArtifactBase`.schema_ Literal подклассы стали бы
принимать чужой тег схемы по одной лишь совместимости типа. Поэтому
`PipelineArtifactBase` — независимый корень с единственным legal-значением
`schema` и тем же контрактом (`frozen`, `extra="forbid"`,
`populate_by_name`, `serialize_by_alias"`), что и `ArtifactBase` — «в том же
духе», но без общего родителя.

Таблица переходов §2 закрыта тройкой `(from, to, reason)`: причины
привязаны к ребру, а не к паре `from → to` — `IDLE → SPEC_LOOP` с чужой
причиной (например, `exported`) отвергается, даже когда ребро само по себе
существует. Все относительные пути (§4.2: «абсолютных путей и
машинно-зависимых значений в манифесте нет») проверяются на уровне поля.
"""

import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from disputatio.contracts.base import ArtifactChild
from disputatio.contracts.session import BudgetUsed

SCHEMA_PIPELINE_V1: Final = "disputatio/pipeline/v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_relative_path(value: str) -> str:
    """Путь манифеста: внутрь корня, POSIX-разделителем, в каноническом виде.

    «Относительный» — не то же самое, что «внутри». `../outside/spec.md`
    относителен и синтаксически безупречен, но склеенный с `workspace_root`
    выводит читателя за пределы репозитория, и проверка одного
    `is_absolute()` пропускала его молча. Поэтому путь разбирается по
    сегментам, и отвергается всё, что не является спуском от корня вниз:

    * пустой (и пробельный) путь и пустой сегмент — `spec//design.md`,
      `spec/` — вид, который не описывает файл;
    * `..` в любом месте — и уводящий наружу, и возвращающийся внутрь:
      `spec/../design.md` равен `design.md` только пока `spec` не
      символическая ссылка, а лексической правды тут недостаточно;
    * `.` — не выход, но и не канонический вид: `Path` его снимает, значит
      в манифест он попасть может только мимо обычного пути записи;
    * разделитель и корень другой ОС — `spec\\design.md`, `C:/repo/spec.md`,
      `C:spec.md`, UNC-форма: §4.2 запрещает машинно-зависимые значения, а
      на POSIX `C:spec.md` — это ещё и легальное имя файла, то есть тихая
      подмена смысла при переносе манифеста.

    Symlink здесь не виден по определению — путь проверяется как текст, без
    файловой системы. Containment после раскрытия ссылок проверяет тот, кто
    резолвит путь в файл (`verifier.doc_gates.resolve_inside` и его
    вызывающие); схема закрывает лексическую половину, и обе нужны.
    """
    if not value.strip():
        raise ValueError(f"path обязан быть непустым: {value!r}")
    if "\\" in value or PureWindowsPath(value).drive:
        raise ValueError(f"path обязан быть POSIX-путём без корня другой ОС: {value!r}")
    if PurePosixPath(value).is_absolute():
        raise ValueError(f"path обязан быть относительным: {value!r}")
    for segment in value.split("/"):
        if not segment.strip() or segment in {".", ".."}:
            raise ValueError(
                f"path обязан вести внутрь корня в каноническом виде "
                f"(сегмент {segment!r} недопустим): {value!r}"
            )
    return value


RelativePath = Annotated[str, AfterValidator(validate_relative_path)]


class PipelinePhase(StrEnum):
    """Фаза пайплайна — все состояния state machine §2 SPEC-002."""

    IDLE = "IDLE"
    SPEC_LOOP = "SPEC_LOOP"
    PAIR_LOOP = "PAIR_LOOP"
    EXPORTING = "EXPORTING"
    ESCALATED = "ESCALATED"
    DONE = "DONE"
    FAILED = "FAILED"


class TransitionReason(StrEnum):
    """Причина перехода — закрытый enum, привязанный к ребру таблицы §2."""

    STARTED = "started"
    SPEC_CONVERGED = "spec_converged"
    PAIR_CONVERGED = "pair_converged"
    ARCHITECTURAL_DEFECT = "architectural_defect"
    EXTERNAL_SPEC_ADOPT = "external_spec_adopt"
    SESSION_DEADLOCK = "session_deadlock"
    SESSION_BUDGET_HIT = "session_budget_hit"
    MAX_ARCHITECTURAL_RETURNS = "max_architectural_returns"
    PIPELINE_BUDGET_HIT = "pipeline_budget_hit"
    EXPORT_PARTIAL = "export_partial"
    EXPORTED = "exported"
    SESSION_FAILED = "session_failed"
    INVARIANT_VIOLATION = "invariant_violation"


# Причины, общие для «любая нетерминальная фаза → ESCALATED» (§2 таблица) —
# один и тот же набор для SPEC_LOOP и PAIR_LOOP, вынесен, чтобы не разойтись.
_ESCALATION_REASONS: Final = frozenset(
    {
        TransitionReason.SESSION_DEADLOCK,
        TransitionReason.SESSION_BUDGET_HIT,
        TransitionReason.MAX_ARCHITECTURAL_RETURNS,
        TransitionReason.PIPELINE_BUDGET_HIT,
    }
)

# Причины перехода в FAILED — общие для любой нетерминальной фазы (§2).
_FAILURE_REASONS: Final = frozenset(
    {TransitionReason.SESSION_FAILED, TransitionReason.INVARIANT_VIOLATION}
)

# DONE и FAILED терминальны — не встречаются как источник ребра (§2).
_NON_TERMINAL_PHASES: Final = (
    PipelinePhase.IDLE,
    PipelinePhase.SPEC_LOOP,
    PipelinePhase.PAIR_LOOP,
    PipelinePhase.EXPORTING,
    PipelinePhase.ESCALATED,
)

ALLOWED_TRANSITIONS: Final[
    dict[tuple[PipelinePhase, PipelinePhase], frozenset[TransitionReason]]
] = {
    (PipelinePhase.IDLE, PipelinePhase.SPEC_LOOP): frozenset(
        {TransitionReason.STARTED}
    ),
    (PipelinePhase.SPEC_LOOP, PipelinePhase.PAIR_LOOP): frozenset(
        {TransitionReason.SPEC_CONVERGED}
    ),
    (PipelinePhase.PAIR_LOOP, PipelinePhase.EXPORTING): frozenset(
        {TransitionReason.PAIR_CONVERGED}
    ),
    (PipelinePhase.PAIR_LOOP, PipelinePhase.SPEC_LOOP): frozenset(
        {
            TransitionReason.ARCHITECTURAL_DEFECT,
            TransitionReason.EXTERNAL_SPEC_ADOPT,
        }
    ),
    (PipelinePhase.SPEC_LOOP, PipelinePhase.ESCALATED): _ESCALATION_REASONS,
    (PipelinePhase.PAIR_LOOP, PipelinePhase.ESCALATED): _ESCALATION_REASONS,
    (PipelinePhase.ESCALATED, PipelinePhase.EXPORTING): frozenset(
        {TransitionReason.EXPORT_PARTIAL}
    ),
    (PipelinePhase.EXPORTING, PipelinePhase.DONE): frozenset(
        {TransitionReason.EXPORTED}
    ),
    **{
        (phase, PipelinePhase.FAILED): _FAILURE_REASONS
        for phase in _NON_TERMINAL_PHASES
    },
}


class SessionOutcome(StrEnum):
    """Pipeline-интерпретация исхода сессии (§4.2 `outcome`).

    Неизменяема после записи.
    """

    CONVERGED = "converged"
    ESCALATED = "escalated"
    FAILED = "failed"
    ARCHITECTURAL_DEFECT = "architectural_defect"
    ABANDONED = "abandoned"


class PipelineArtifactBase(BaseModel):
    """Корень артефактов семейства `disputatio/pipeline/v1` (§4.2).

    Единственное значение схемы в семействе не нуждается в выборе между
    версиями — в отличие от `ArtifactBase`, где дефолт `disputatio/v1`
    существует ради старых develop/analyze вызывающих мест. Но конструктор
    и парсинг обязаны различаться так же, как у `ArtifactBase` (см.
    `base.py`): `pipeline.json` читается с диска на каждом resume (§8), и
    именно там отсутствующий/повреждённый ключ `schema` обязан падать
    `ValidationError`, а не тихо доопределяться значением по умолчанию —
    иначе манифест без тега схемы читался бы как валидный. Поэтому default
    живёт не в самом поле (`Field(default=...)` сработал бы одинаково и в
    конструкторе, и в `model_validate`), а в кастомном `__init__`: маркер
    `__pydantic_base_init__` возвращает `model_validate` на прямой путь
    мимо `__init__`, где `schema_` — обязательное поле без дефолта.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["disputatio/pipeline/v1"] = Field(
        alias="schema", serialization_alias="schema"
    )

    def __init__(self, /, **data: Any) -> None:
        # Конструктор — единственный путь с default-подстановкой (удобство
        # для программного кода пайплайна); model_validate — строгий путь
        # для payload'ов с диска (§8 SPEC-002).
        if "schema" not in data and "schema_" not in data:
            data["schema"] = SCHEMA_PIPELINE_V1
        super().__init__(**data)

    # Без маркера pydantic-core прогнал бы model_validate через этот же
    # __init__ — подстановка сработала бы и при парсинге. С маркером
    # (как у ArtifactBase.__init__) валидация идёт прямым путём мимо него.
    __init__.__pydantic_base_init__ = True  # type: ignore[missing-attribute]


class FileRef(ArtifactChild):
    """Ссылка на файл-снапшот верхнего уровня: путь + sha256.

    §4.2 `task`/`config`/`checklists`.
    """

    path: RelativePath
    sha256: str


class DocumentPaths(ArtifactChild):
    """Канонические пути редактируемой пары (§4.2 `documents`)."""

    spec_path: RelativePath
    plan_path: RelativePath


class EvidenceLink(ArtifactChild):
    """Структурированная ссылка на находку раунда сессии.

    §4.2 `transitions[].evidence`.
    """

    session_id: str
    round: int = Field(ge=1)
    finding_id: str


class SessionRecord(ArtifactChild):
    """Запись об одной ревизии spec- или pair-сессии.

    §4.2 `spec_sessions`/`pair_sessions`.
    """

    revision: int = Field(ge=1)
    session_id: str
    path: RelativePath
    entry_hashes: dict[str, str]
    outcome: SessionOutcome | None = None
    superseded_by: str | None = None

    @model_validator(mode="after")
    def _validate_entry_hashes(self) -> "SessionRecord":
        # Значение — либо sha256 документа на входе сессии, либо явный
        # маркер "absent" (план законно отсутствует в spec-r1, §4.2).
        for doc_path, digest in self.entry_hashes.items():
            if digest != "absent" and not _SHA256_RE.fullmatch(digest):
                raise ValueError(
                    f"entry_hashes[{doc_path!r}] обязан быть sha256-хексом либо "
                    f'литералом "absent": {digest!r}'
                )
        return self


class Transition(ArtifactChild):
    """Один переход state machine пайплайна (§2, §4.2 `transitions`)."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    from_: PipelinePhase = Field(alias="from", serialization_alias="from")
    to: PipelinePhase
    reason: TransitionReason
    evidence: list[EvidenceLink] = Field(default_factory=list)
    at: datetime

    @model_validator(mode="after")
    def _validate_against_table(self) -> "Transition":
        allowed = ALLOWED_TRANSITIONS.get((self.from_, self.to))
        if allowed is None:
            raise ValueError(
                f"переход {self.from_.value} → {self.to.value} отсутствует "
                "в закрытой таблице §2"
            )
        if self.reason not in allowed:
            raise ValueError(
                f"причина {self.reason.value!r} не допущена для ребра "
                f"{self.from_.value} → {self.to.value} (§2)"
            )
        return self


class OperatorDecision(ArtifactChild):
    """Provenance вмешательства оператора (§3.1, §4.2 `operator_decisions`)."""

    operation_id: str
    kind: Literal["discard_round", "adopt_external"]
    at: datetime
    worktree_diff_sha256: str


class NextAction(ArtifactChild):
    """Write-ahead intent runner'а (§4.3 `next_action`)."""

    operation_id: str
    kind: Literal[
        "create_session",
        "run_session",
        "finish_session",
        "record_return",
        "adopt_external",
        "discard_round",
        "export",
    ]
    args: dict[str, Any] = Field(default_factory=dict)
    predecessor_operation_id: str | None = None


class AppendOnlyEntry(ArtifactChild):
    """Prefix-снапшот одного append-only журнала (§2 P9, §4.2 `append_only`)."""

    prefix_bytes: int = Field(ge=0)
    prefix_sha256: str


class IntegritySnapshot(ArtifactChild):
    """Запись `integrity_anchor` (§2 P9) — в манифест пайплайна не входит.

    Модель объявлена здесь как часть семейства схемы, но живёт только в
    append-only журнале анкера (`IntegrityAnchor`, задача 6); JSON-форма
    ровно как в §4.2: неизменяемые файлы — плоское `{path: sha256}`,
    журналы — `{path: {prefix_bytes, prefix_sha256}}`.
    """

    session_id: str
    round: int = Field(ge=1)
    operation_id: str
    immutable: dict[str, str] = Field(default_factory=dict)
    append_only: dict[str, AppendOnlyEntry] = Field(default_factory=dict)


class PipelineState(PipelineArtifactBase):
    """Корневой артефакт `pipeline.json` (§4.2 SPEC-002) — текущее состояние.

    Append-only в нём — исторические коллекции (`spec_sessions`,
    `pair_sessions`, `transitions`, `operator_decisions`); файл в целом
    перезаписывается атомарно целиком (temp-file + rename) — гарантия
    append-only коллекций и разрешённая правка `outcome`/`superseded_by`
    задним числом — обязанность стора (задача 5), не схемы.
    """

    pipeline_id: str
    created_at: datetime
    phase: PipelinePhase
    task: FileRef
    config: FileRef
    checklists: FileRef
    documents: DocumentPaths
    spec_sessions: list[SessionRecord] = Field(default_factory=list)
    pair_sessions: list[SessionRecord] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    budget_used: BudgetUsed
    operator_decisions: list[OperatorDecision] = Field(default_factory=list)
    anchor_id: str
    next_action: NextAction | None = None
