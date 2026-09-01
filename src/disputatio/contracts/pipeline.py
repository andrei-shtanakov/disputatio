"""Манифест пайплайна — семейство схемы `disputatio/pipeline/v1`+`v2` (SPEC-002).

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
SCHEMA_PIPELINE_V2: Final = "disputatio/pipeline/v2"

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


class PipelineKind(StrEnum):
    """Вид пайплайна (§1 SPEC-002): набор контуров и доступные рёбра.

    Видов ровно два, и третий потребует такого же явного изменения спеки:
    обобщённого N-stage DSL здесь нет намеренно (§1).
    """

    PAIR = "pair"
    DOCUMENT = "document"


class PipelinePhase(StrEnum):
    """Фаза пайплайна — все состояния state machine §2 SPEC-002."""

    IDLE = "IDLE"
    SPEC_LOOP = "SPEC_LOOP"
    PAIR_LOOP = "PAIR_LOOP"
    DOC_LOOP = "DOC_LOOP"
    EXPORTING = "EXPORTING"
    ESCALATED = "ESCALATED"
    DONE = "DONE"
    FAILED = "FAILED"


class TransitionReason(StrEnum):
    """Причина перехода — закрытый enum, привязанный к ребру таблицы §2."""

    STARTED = "started"
    SPEC_CONVERGED = "spec_converged"
    PAIR_CONVERGED = "pair_converged"
    DOCUMENT_CONVERGED = "document_converged"
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

# Причины эскалации вида `document` (§2): `max_architectural_returns` в них
# НЕ входит и общий набор не переиспользуется — возвратов у вида нет, и
# причина, которая не может наступить, в допустимом наборе была бы
# приглашением записать её по ошибке.
_DOC_ESCALATION_REASONS: Final = frozenset(
    {
        TransitionReason.SESSION_DEADLOCK,
        TransitionReason.SESSION_BUDGET_HIT,
        TransitionReason.PIPELINE_BUDGET_HIT,
    }
)

# DONE и FAILED терминальны — не встречаются как источник ребра (§2).
_NON_TERMINAL_PHASES: Final = (
    PipelinePhase.IDLE,
    PipelinePhase.SPEC_LOOP,
    PipelinePhase.PAIR_LOOP,
    PipelinePhase.DOC_LOOP,
    PipelinePhase.EXPORTING,
    PipelinePhase.ESCALATED,
)

ALLOWED_TRANSITIONS: Final[
    dict[tuple[PipelinePhase, PipelinePhase], frozenset[TransitionReason]]
] = {
    (PipelinePhase.IDLE, PipelinePhase.SPEC_LOOP): frozenset(
        {TransitionReason.STARTED}
    ),
    (PipelinePhase.IDLE, PipelinePhase.DOC_LOOP): frozenset({TransitionReason.STARTED}),
    (PipelinePhase.SPEC_LOOP, PipelinePhase.PAIR_LOOP): frozenset(
        {TransitionReason.SPEC_CONVERGED}
    ),
    (PipelinePhase.DOC_LOOP, PipelinePhase.EXPORTING): frozenset(
        {TransitionReason.DOCUMENT_CONVERGED}
    ),
    (PipelinePhase.DOC_LOOP, PipelinePhase.ESCALATED): _DOC_ESCALATION_REASONS,
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
    """Корень артефактов семейства `disputatio/pipeline/v1`+`v2` (§4.2).

    Версий в семействе две, но выбора между ними у писателя нет: пишется
    всегда v2 (§4.2), v1 только читается — и читается нормализацией по тегу
    в `PipelineState`, а не дефолтом внутри модели. Конструктор
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

    schema_: Literal["disputatio/pipeline/v1", "disputatio/pipeline/v2"] = Field(
        alias="schema", serialization_alias="schema"
    )

    def __init__(self, /, **data: Any) -> None:
        # Конструктор — единственный путь с default-подстановкой (удобство
        # для программного кода пайплайна); model_validate — строгий путь
        # для payload'ов с диска (§8 SPEC-002). Подставляется v2: новые
        # манифесты пишутся v2 для ОБОИХ видов (§4.2), а v1 только читается.
        if "schema" not in data and "schema_" not in data:
            data["schema"] = SCHEMA_PIPELINE_V2
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


class PairDocuments(ArtifactChild):
    """Пара редактируемых документов (§4.2, `documents.kind = "pair"`).

    Дефолт у `kind` есть, но совместимость держит НЕ он: тег-union выбирает
    ветку до валидации членов, и payload без дискриминатора отвергается
    `union_tag_not_found`. Дефолт нужен лишь программному конструированию
    внутри runner'а; чтение старых файлов чинит нормализация по тегу схемы
    в `PipelineState`.
    """

    kind: Literal["pair"] = "pair"
    spec_path: RelativePath
    plan_path: RelativePath

    def paths(self) -> tuple[str, ...]:
        """Документы вида в каноническом порядке — спека, затем план."""
        return (self.spec_path, self.plan_path)


class SingleDocument(ArtifactChild):
    """Единственный редактируемый документ (§4.2, `kind = "document"`).

    Дефолта у `kind` здесь нет намеренно: он и есть признак, по которому
    union выбирает эту ветку, а «документ по умолчанию» сделал бы форму
    пары неотличимой от неполной документной.
    """

    kind: Literal["document"]
    document_path: RelativePath

    def paths(self) -> tuple[str, ...]:
        """Документы вида: ровно один."""
        return (self.document_path,)


#: Дискриминированный union формы документов (§4.2). Опциональные поля здесь
#: были бы тем самым `plan_path = null`, от которого вид `document` уходит по
#: построению (P10): форма манифеста обязана делать «документный пайплайн с
#: путём плана» НЕВЫРАЗИМЫМ, а не полагаться на то, что его никто не запишет.
Documents = Annotated[PairDocuments | SingleDocument, Field(discriminator="kind")]

#: Контуры каждого вида в порядке их прохождения (§1, §2).
CONTOURS_BY_KIND: Final[dict[PipelineKind, tuple[str, ...]]] = {
    PipelineKind.PAIR: ("spec", "pair"),
    PipelineKind.DOCUMENT: ("doc",),
}

#: Контур, сходимость которого терминальна для вида (§7.2).
TERMINAL_CONTOUR: Final[dict[PipelineKind, str]] = {
    PipelineKind.PAIR: "pair",
    PipelineKind.DOCUMENT: "doc",
}

#: Фаза, в которую вид входит из `IDLE` (§2).
ENTRY_PHASE: Final[dict[PipelineKind, PipelinePhase]] = {
    PipelineKind.PAIR: PipelinePhase.SPEC_LOOP,
    PipelineKind.DOCUMENT: PipelinePhase.DOC_LOOP,
}

#: Единственный источник имени коллекции сессий по контуру (§4.2). Перечисление,
#: которое забывают дополнить, обязано перестать быть перечислением: третий
#: контур уже показал, что литеральные списки коллекций расходятся молча.
SESSIONS_FIELD_BY_CONTOUR: Final[dict[str, str]] = {
    "spec": "spec_sessions",
    "pair": "pair_sessions",
    "doc": "doc_sessions",
}

#: Фазы, принадлежащие каждому виду. Отсюда выводятся и FAILED-рёбра:
#: у вида нет своей фазы — нет и перехода из неё, даже в FAILED.
PHASES_BY_KIND: Final[dict[PipelineKind, tuple[PipelinePhase, ...]]] = {
    PipelineKind.PAIR: (PipelinePhase.SPEC_LOOP, PipelinePhase.PAIR_LOOP),
    PipelineKind.DOCUMENT: (PipelinePhase.DOC_LOOP,),
}

#: Нетерминальные фазы, общие обоим видам.
_COMMON_PHASES: Final = (
    PipelinePhase.IDLE,
    PipelinePhase.EXPORTING,
    PipelinePhase.ESCALATED,
)

_SHARED_EDGES: Final = frozenset(
    {
        (PipelinePhase.ESCALATED, PipelinePhase.EXPORTING),
        (PipelinePhase.EXPORTING, PipelinePhase.DONE),
        *((phase, PipelinePhase.FAILED) for phase in _COMMON_PHASES),
    }
)

_OWN_EDGES: Final[
    dict[PipelineKind, frozenset[tuple[PipelinePhase, PipelinePhase]]]
] = {
    PipelineKind.PAIR: frozenset(
        {
            (PipelinePhase.IDLE, PipelinePhase.SPEC_LOOP),
            (PipelinePhase.SPEC_LOOP, PipelinePhase.PAIR_LOOP),
            (PipelinePhase.PAIR_LOOP, PipelinePhase.EXPORTING),
            (PipelinePhase.PAIR_LOOP, PipelinePhase.SPEC_LOOP),
            (PipelinePhase.SPEC_LOOP, PipelinePhase.ESCALATED),
            (PipelinePhase.PAIR_LOOP, PipelinePhase.ESCALATED),
        }
    ),
    PipelineKind.DOCUMENT: frozenset(
        {
            (PipelinePhase.IDLE, PipelinePhase.DOC_LOOP),
            (PipelinePhase.DOC_LOOP, PipelinePhase.EXPORTING),
            (PipelinePhase.DOC_LOOP, PipelinePhase.ESCALATED),
        }
    ),
}

#: Рёбра, доступные виду (§2, колонка «Вид»). Общая `ALLOWED_TRANSITIONS`
#: описывает машину целиком — и это верно; сужает её вот эта таблица, и
#: сужение обязано доходить до `FAILED`: «любая нетерминальная фаза → FAILED»
#: означает любую фазу СВОЕГО вида.
EDGES_BY_KIND: Final[
    dict[PipelineKind, frozenset[tuple[PipelinePhase, PipelinePhase]]]
] = {
    kind: own
    | _SHARED_EDGES
    | frozenset((phase, PipelinePhase.FAILED) for phase in PHASES_BY_KIND[kind])
    for kind, own in _OWN_EDGES.items()
}


class EvidenceLink(ArtifactChild):
    """Структурированная ссылка на находку раунда сессии.

    §4.2 `transitions[].evidence`.
    """

    session_id: str
    round: int = Field(ge=1)
    finding_id: str


class SessionRecord(ArtifactChild):
    """Запись об одной ревизии контура.

    §4.2 `spec_sessions`/`pair_sessions`/`doc_sessions`.
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
    `pair_sessions`, `doc_sessions`, `transitions`, `operator_decisions`);
    файл в целом
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
    documents: Documents
    spec_sessions: list[SessionRecord] = Field(default_factory=list)
    pair_sessions: list[SessionRecord] = Field(default_factory=list)
    doc_sessions: list[SessionRecord] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    budget_used: BudgetUsed
    operator_decisions: list[OperatorDecision] = Field(default_factory=list)
    anchor_id: str
    next_action: NextAction | None = None

    @property
    def kind(self) -> PipelineKind:
        """Вид читается из дискриминатора и больше ниоткуда (§4.2).

        Второе поле верхнего уровня пришлось бы сверять с этим при каждом
        чтении, а расхождение двух записей об одном факте нечем разрешить.
        """
        return PipelineKind(self.documents.kind)

    @model_validator(mode="before")
    @classmethod
    def _normalize_v1_documents(cls, data: Any) -> Any:
        """Совместимость с v1 — до выбора ветки union, а не дефолтом в ней.

        Тег-union pydantic извлекает дискриминатор раньше, чем валидирует
        члена, поэтому `documents` без `kind` отвергается
        `union_tag_not_found` независимо от значений по умолчанию внутри
        `PairDocuments`. Дописать же `kind` в файл под тегом v1 нельзя:
        `extra="forbid"` базовой модели — значит строгий читатель v1
        отвергнет такой файл. Отсюда правило §4.2: v1 без `kind`, всякий
        файл с `kind` — v2.

        Подъём тега прямо здесь — не побочный эффект: прочитанный
        v1-манифест уже представлен в памяти v2-формой, и оставить ему
        прежний тег значило бы записать обратно файл, чья форма не
        совпадает с объявленной.
        """
        if not isinstance(data, dict):
            return data
        if data.get("schema") != SCHEMA_PIPELINE_V1:
            return data
        documents = data.get("documents")
        if not isinstance(documents, dict):
            return data
        if "kind" in documents:
            raise ValueError(
                "манифест с тегом disputatio/pipeline/v1 несёт "
                "documents.kind: версия v1 заморожена без этого поля, файл "
                "лжёт о своей форме (§4.2)"
            )
        return {
            **data,
            "schema": SCHEMA_PIPELINE_V2,
            "documents": {**documents, "kind": "pair"},
        }

    @model_validator(mode="after")
    def _validate_kind_consistency(self) -> "PipelineState":
        """Коллекции, рёбра и тег схемы обязаны принадлежать виду (§2, §4.2).

        Проверка живёт здесь, а не в `Transition`: ребро само по себе вида
        не знает и знать не может — вид записан в `documents`, то есть на
        уровень выше. Без этого валидатора `EDGES_BY_KIND` осталась бы
        объявленной, но мёртвой таблицей.
        """
        own = {SESSIONS_FIELD_BY_CONTOUR[c] for c in CONTOURS_BY_KIND[self.kind]}
        for contour, field_name in SESSIONS_FIELD_BY_CONTOUR.items():
            if field_name in own:
                continue
            if getattr(self, field_name):
                raise ValueError(
                    f"{field_name}: непустая коллекция сессий чужого вида "
                    f"(контур {contour!r} не принадлежит виду "
                    f"{self.kind.value!r})"
                )
        allowed_edges = EDGES_BY_KIND[self.kind]
        for transition in self.transitions:
            edge = (transition.from_, transition.to)
            if edge not in allowed_edges:
                raise ValueError(
                    f"переход {transition.from_.value} → {transition.to.value} "
                    f"чужое виду {self.kind.value!r} ребро: таблица §2 его "
                    "допускает, но не для этого вида пайплайна"
                )
        if self.kind is PipelineKind.DOCUMENT and self.schema_ != SCHEMA_PIPELINE_V2:
            raise ValueError(
                "пайплайн вида document обязан нести схему "
                f"{SCHEMA_PIPELINE_V2!r}: фаза DOC_LOOP и причина "
                "document_converged несовместимы со строгим читателем v1"
            )
        return self
