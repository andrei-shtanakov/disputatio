# План имплементации SPEC-002 v0.2 — вид пайплайна `document`

> **Для agentic-исполнителей:** REQUIRED SUB-SKILL:
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans — задача за задачей, шаги — чекбоксы `- [ ]`.

**Цель:** добавить второй вид пайплайна — `document`, полирующий **один**
документ одним контуром `doc`, — не тронув поведение существующего вида `pair`.

**Архитектура:** вид выводится из формы секции `[pipeline]` и записывается в
манифест дискриминатором `documents.kind`. Движок раунда, пять doc-гейтов, анкер
целостности P9, снапшоты, бюджет, журнал событий и resume переиспользуются
целиком. Механика, принадлежащая виду `pair` (политика границы раунда, путь
возврата, `defect_class`, потолок возвратов), у вида `document` **не
конструируется** (P10) — ветка выбирается в composition root, а не условием
внутри runner'а.

**Стек:** Python 3.12+, pydantic, pytest (anyio), uv, pyrefly, ruff.

**Спека:** `disputatio-SPEC-002-doc-pipeline.md` редакции **v0.2**. Границы
решения и инвентарь швов — `docs/plans/2026-08-31-document-pipeline-kind-spike.md`.
План аргументирует от спеки; исполнитель читает оба документа.

## Глобальные ограничения

- Пакетный менеджер — только `uv`; тесты — `uv run pytest -q`.
- После каждой задачи: `uv run ruff format . && uv run ruff check .`,
  `uv run pyrefly check`, полный suite зелёный. Строка ≤ 88.
- `core/deciding.py` и `core/machine.py` **не редактируются ни в одной задаче**
  (SPEC-002 V6/§7.1); правка в них = ошибка декомпозиции плана.
- **Вид `pair` не меняет поведения ни в одной задаче.** Допустимых отличий
  ровно два, оба в сериализации манифеста и оба объявлены в §4.2: тег схемы
  `disputatio/pipeline/v1 → v2` и появление `documents.kind = "pair"`. Они
  приходят парой и порознь невозможны: файл с `kind` под тегом v1 запрещён
  контрактом, а тег v2 без `kind` не прошёл бы дискриминацию. Любое другое
  расхождение — регрессия, а не «побочный эффект рефакторинга».
- Механика чужого вида **не конструируется** (P10). Проверка на ревью: если в
  диффе появилось `if kind == ...` внутри `PipelineRunner` там, где можно было
  выбрать объект в `build_pipeline`, — это отклонение от P10.
- Владение кодом — по §9 SPEC-002 (инварианты D1): модели/порты → `contracts`,
  файловый I/O → `events`, гейты → `verifier`, промпты → `context`,
  оркестрация/composition → `runtime`.
- Коммит после каждой задачи: `feat(contracts): …` / `feat(pipeline): …` по
  пакету задачи; TDD — red-тест до реализации.
- Русский язык докстрингов и сообщений об ошибках — как в существующем коде.
- **Меняешь сигнатуру — сначала посчитай вызовы.** Пять из шести дефектов,
  найденных ревью этой пары, были одного класса: обязательное изменение,
  чьи существующие call site'ы не попали в область задачи. Перед реализацией
  каждой задачи прогнать `grep -rn "<имя>(" src tests` и сверить с её списком
  файлов; расхождение — дефект плана, о нём сообщить, а не молча дописать
  дефолт, гасящий ошибку.

---

### Задача 1: contracts — вид пайплайна в схеме манифеста

**Файлы:**
- Modify: `src/disputatio/contracts/pipeline.py`
- Modify: `src/disputatio/contracts/__init__.py` (реэкспорт новых имён)
- Modify: **все потребители имени `DocumentPaths`** — оно переименовывается,
  алиаса не остаётся: `src/disputatio/runtime/pipeline_runner.py` (2),
  `tests/contracts/test_pipeline_state.py` (5),
  `tests/runtime/test_pipeline_export.py` (2), `tests/contracts/test_init.py` (1)
- Modify: **все читатели `documents.spec_path`/`.plan_path`** — после union
  этих полей у ветки `SingleDocument` нет, и `pyrefly` краснеет немедленно:
  `pipeline_runner.py:1236`, `pipeline_export.py:291,300,301`, `cli.py:409`
- Test: `tests/contracts/test_pipeline_kind.py`

**Интерфейсы (производит):**

```python
class PipelineKind(StrEnum):
    PAIR = "pair"
    DOCUMENT = "document"

class PairDocuments(ArtifactChild):
    kind: Literal["pair"] = "pair"
    spec_path: RelativePath
    plan_path: RelativePath

class SingleDocument(ArtifactChild):
    kind: Literal["document"]
    document_path: RelativePath

Documents = Annotated[
    PairDocuments | SingleDocument, Field(discriminator="kind")
]

CONTOURS_BY_KIND: Final[dict[PipelineKind, tuple[str, ...]]]
TERMINAL_CONTOUR: Final[dict[PipelineKind, str]]
ENTRY_PHASE: Final[dict[PipelineKind, PipelinePhase]]
EDGES_BY_KIND: Final[
    dict[PipelineKind, frozenset[tuple[PipelinePhase, PipelinePhase]]]
]
SESSIONS_FIELD_BY_CONTOUR: Final[dict[str, str]]
SCHEMA_PIPELINE_V2: Final = "disputatio/pipeline/v2"
```

`PipelinePhase.DOC_LOOP`, `TransitionReason.DOCUMENT_CONVERGED`,
`PipelineState.doc_sessions`, `PipelineState.kind` (property, читает
`documents.kind`).

**Почему `documents` — union, а не опциональные поля.** «Документный пайплайн с
`plan_path`» обязан быть невыразим схемой, а не просто не встречаться (§4.2,
P10). `DocumentPaths` переименовывается в `PairDocuments`. **Алиаса старого
имени не остаётся**, поэтому все четыре потребителя правятся этой же задачей —
иначе её коммит не может закончиться зелёным suite.

**Расширение типа и миграция читателей — одна атомарная единица.** Как только
`documents` становится union'ом, всякое обращение к `.spec_path`/`.plan_path`
перестаёт типизироваться: у ветки `SingleDocument` таких полей нет. Отложить
эти пять мест до задач 5 и 7, как было в первой редакции плана, значит оставить
`pyrefly check` красным на две задачи вперёд — а он объявлен обязательным после
каждой. Поэтому union приходит вместе с общим аксессором, и читатели переходят
на него здесь же:

```python
class PairDocuments(ArtifactChild):
    def paths(self) -> tuple[str, ...]:
        return (self.spec_path, self.plan_path)


class SingleDocument(ArtifactChild):
    def paths(self) -> tuple[str, ...]:
        return (self.document_path,)
```

Три из пяти мест получают окончательную форму и больше не трогаются:
`_entry_hashes` итерирует `state.documents.paths()` (задача 5 его уже не
касается), `_pr_title` и `cli.py:409` собирают
`" + ".join(state.documents.paths())` — для пары это байт-в-байт прежняя
строка. Оставшиеся две (`pipeline_export.py:300-301`, прозаические «Спека:» и
«План:») в этой задаче становятся общим списком документов, а окончательную
формулировку по виду им даёт задача 7. Эта двухшаговость объявлена
намеренно: две строки переписываются дважды, зато ни одна задача не
заканчивается красным типчекером.

**Совместимость — нормализация по тегу, а НЕ дефолт внутри модели.** Проверено
экспериментом: тег-union pydantic выбирает ветку до валидации её членов, поэтому
payload без `kind` отвергается `union_tag_not_found`, сколько бы значений по
умолчанию ни стояло в `PairDocuments.kind`. Одновременно дописать `kind` в файл
под тегом v1 нельзя: базовая модель несёт `extra="forbid"`, и строгий читатель
v1 отвергнет такой файл `extra_forbidden`. Отсюда контракт §4.2: v1 заморожена
без `kind`, всякий файл с `kind` — v2, пишется всегда v2, а чтение v1 идёт через
`mode="before"`-нормализацию.

- [ ] **Шаг 1: red-тест — union документов и запрет чужой формы**

```python
# tests/contracts/test_pipeline_kind.py
import pytest
from pydantic import ValidationError

from disputatio.contracts.pipeline import (
    PairDocuments,
    PipelineKind,
    PipelinePhase,
    SingleDocument,
    TransitionReason,
)


def test_v1_payload_without_kind_reads_as_pair() -> None:
    """Манифест v0.1 поля kind не несёт — нормализуется по тегу, не дефолтом."""
    state = PipelineState.model_validate(_pair_payload("disputatio/pipeline/v1"))
    assert state.kind is PipelineKind.PAIR


def test_v1_payload_carrying_kind_is_rejected() -> None:
    """Файл, лгущий о своей форме, проходить не должен."""
    payload = _pair_payload("disputatio/pipeline/v1")
    payload["documents"]["kind"] = "pair"
    with pytest.raises(ValidationError, match="v1"):
        PipelineState.model_validate(payload)


def test_every_write_carries_v2_tag() -> None:
    """Пара, заведённая как v1, при первой же записи объявляет форму честно."""
    state = PipelineState.model_validate(_pair_payload("disputatio/pipeline/v1"))
    assert state.model_dump(mode="json")["schema"] == "disputatio/pipeline/v2"


def test_single_document_rejects_plan_path() -> None:
    """«Документный пайплайн с планом» невыразим схемой, а не редок."""
    with pytest.raises(ValidationError):
        SingleDocument.model_validate(
            {
                "kind": "document",
                "document_path": "docs/charter.md",
                "plan_path": "docs/plan.md",
            }
        )


def test_document_kind_has_own_entry_phase_and_terminal_contour() -> None:
    from disputatio.contracts.pipeline import (
        CONTOURS_BY_KIND,
        ENTRY_PHASE,
        TERMINAL_CONTOUR,
    )

    assert CONTOURS_BY_KIND[PipelineKind.DOCUMENT] == ("doc",)
    assert ENTRY_PHASE[PipelineKind.DOCUMENT] is PipelinePhase.DOC_LOOP
    assert TERMINAL_CONTOUR[PipelineKind.DOCUMENT] == "doc"


def test_doc_loop_converged_edge_exists_and_is_document_only() -> None:
    from disputatio.contracts.pipeline import ALLOWED_TRANSITIONS, EDGES_BY_KIND

    edge = (PipelinePhase.DOC_LOOP, PipelinePhase.EXPORTING)
    assert TransitionReason.DOCUMENT_CONVERGED in ALLOWED_TRANSITIONS[edge]
    assert edge in EDGES_BY_KIND[PipelineKind.DOCUMENT]
    assert edge not in EDGES_BY_KIND[PipelineKind.PAIR]


def test_doc_loop_escalation_excludes_architectural_returns() -> None:
    """Причина, которая не может наступить, в наборе — приглашение к ошибке."""
    from disputatio.contracts.pipeline import ALLOWED_TRANSITIONS

    reasons = ALLOWED_TRANSITIONS[
        (PipelinePhase.DOC_LOOP, PipelinePhase.ESCALATED)
    ]
    assert TransitionReason.MAX_ARCHITECTURAL_RETURNS not in reasons
    assert TransitionReason.SESSION_DEADLOCK in reasons
```

- [ ] **Шаг 2: прогнать, убедиться что падает**

Run: `uv run pytest tests/contracts/test_pipeline_kind.py -q`
Ожидание: FAIL — `ImportError: cannot import name 'PairDocuments'`.

- [ ] **Шаг 3: реализация в `contracts/pipeline.py`**

```python
SCHEMA_PIPELINE_V2: Final = "disputatio/pipeline/v2"


class PipelineKind(StrEnum):
    """Вид пайплайна (§1 SPEC-002): набор контуров и доступные рёбра."""

    PAIR = "pair"
    DOCUMENT = "document"


class PairDocuments(ArtifactChild):
    """Пара редактируемых документов (§4.2, `documents.kind = "pair"`).

    Дефолт у `kind` есть, но совместимость держит НЕ он: тег-union выбирает
    ветку до валидации членов, и payload без дискриминатора отвергается
    `union_tag_not_found`. Дефолт нужен лишь программному конструированию
    внутри runner'а; чтение старых файлов чинит нормализация по тегу схемы
    в `PipelineState` (ниже).
    """

    kind: Literal["pair"] = "pair"
    spec_path: RelativePath
    plan_path: RelativePath


class SingleDocument(ArtifactChild):
    """Единственный редактируемый документ (§4.2, `kind = "document"`).

    Дефолта у `kind` здесь нет намеренно: он и есть признак, по которому
    union выбирает эту ветку, а «документ по умолчанию» сделал бы форму
    пары неотличимой от неполной документной.
    """

    kind: Literal["document"]
    document_path: RelativePath


Documents = Annotated[
    PairDocuments | SingleDocument, Field(discriminator="kind")
]

CONTOURS_BY_KIND: Final[dict[PipelineKind, tuple[str, ...]]] = {
    PipelineKind.PAIR: ("spec", "pair"),
    PipelineKind.DOCUMENT: ("doc",),
}

TERMINAL_CONTOUR: Final[dict[PipelineKind, str]] = {
    PipelineKind.PAIR: "pair",
    PipelineKind.DOCUMENT: "doc",
}

ENTRY_PHASE: Final[dict[PipelineKind, PipelinePhase]] = {
    PipelineKind.PAIR: PipelinePhase.SPEC_LOOP,
    PipelineKind.DOCUMENT: PipelinePhase.DOC_LOOP,
}

SESSIONS_FIELD_BY_CONTOUR: Final[dict[str, str]] = {
    "spec": "spec_sessions",
    "pair": "pair_sessions",
    "doc": "doc_sessions",
}
```

В `PipelinePhase` добавить `DOC_LOOP = "DOC_LOOP"`, в `TransitionReason` —
`DOCUMENT_CONVERGED = "document_converged"`, в `_NON_TERMINAL_PHASES` —
`PipelinePhase.DOC_LOOP`.

В `ALLOWED_TRANSITIONS` добавить три ребра:

```python
    (PipelinePhase.IDLE, PipelinePhase.DOC_LOOP): frozenset(
        {TransitionReason.STARTED}
    ),
    (PipelinePhase.DOC_LOOP, PipelinePhase.EXPORTING): frozenset(
        {TransitionReason.DOCUMENT_CONVERGED}
    ),
    (PipelinePhase.DOC_LOOP, PipelinePhase.ESCALATED): frozenset(
        {
            TransitionReason.SESSION_DEADLOCK,
            TransitionReason.SESSION_BUDGET_HIT,
            TransitionReason.PIPELINE_BUDGET_HIT,
        }
    ),
```

`_ESCALATION_REASONS` (с `MAX_ARCHITECTURAL_RETURNS`) остаётся набором рёбер
`SPEC_LOOP`/`PAIR_LOOP` и для `DOC_LOOP` **не переиспользуется**: у вида
`document` возвратов нет, и общий набор внёс бы причину, которая не наступает.

Принадлежность рёбер видам:

```python
EDGES_BY_KIND: Final[
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
            *_SHARED_EDGES,
        }
    ),
    PipelineKind.DOCUMENT: frozenset(
        {
            (PipelinePhase.IDLE, PipelinePhase.DOC_LOOP),
            (PipelinePhase.DOC_LOOP, PipelinePhase.EXPORTING),
            (PipelinePhase.DOC_LOOP, PipelinePhase.ESCALATED),
            *_SHARED_EDGES,
        }
    ),
}
```

где `_SHARED_EDGES` — `(ESCALATED, EXPORTING)`, `(EXPORTING, DONE)` и все
`(phase, FAILED)`.

**Таблицу обязательно ПОДКЛЮЧИТЬ к валидации, а не только объявить.**
`Transition._validate_against_table` вида не знает и знать не может — вид
живёт в `PipelineState`. Поэтому проверка принадлежности ребра виду идёт
model-валидатором состояния (шаг 7), а тест на неё — негативный: документный
манифест с ребром `SPEC_LOOP → PAIR_LOOP` обязан **не читаться**. Тест,
проверяющий лишь `edge in EDGES_BY_KIND[...]`, доказывал бы существование
таблицы и ничего не говорил бы о том, что ею кто-то пользуется.

- [ ] **Шаг 4: прогнать — тесты проходят**

Run: `uv run pytest tests/contracts/test_pipeline_kind.py -q`
Ожидание: PASS.

- [ ] **Шаг 5: red-тест — `PipelineState` под вид**

```python
def test_state_rejects_transition_of_foreign_kind() -> None:
    """Ребро, допустимое общей таблицей, но чужое виду, отвергается (§2).

    Проверка членства в `EDGES_BY_KIND` доказывала бы только объявление
    таблицы. Здесь проверяется её ПРИМЕНЕНИЕ: документный манифест с
    ребром pair-механики не должен читаться вовсе — иначе таблица остаётся
    мёртвой при зелёном тесте.
    """
    with pytest.raises(ValidationError, match="чужое виду"):
        _document_state(
            transitions=[
                {
                    "from": "SPEC_LOOP",
                    "to": "PAIR_LOOP",
                    "reason": "spec_converged",
                    "at": "2026-08-31T00:00:00Z",
                }
            ]
        )


def test_pair_state_accepts_its_own_edges() -> None:
    """Регрессия: у пары те же рёбра принимаются как раньше."""
    state = PipelineState.model_validate(_pair_payload_with_spec_converged())
    assert state.transitions[-1].to is PipelinePhase.PAIR_LOOP


def test_state_rejects_sessions_of_foreign_kind() -> None:
    """Непустая коллекция чужого вида — invariant_violation, не «лишние данные»."""
    with pytest.raises(ValidationError, match="чужого вида"):
        _document_state(
            pair_sessions=[
                {
                    "revision": 1,
                    "session_id": "pair-r1",
                    "path": "sessions/pair-r1",
                    "entry_hashes": {},
                }
            ]
        )


def test_document_state_requires_v2_schema() -> None:
    with pytest.raises(ValidationError, match="disputatio/pipeline/v2"):
        _document_state(schema="disputatio/pipeline/v1")

```

(Чтение v1 и отказ на лгущем теге проверены тестами шага 1; здесь — только
инварианты вида и обязательность v2 для документного пайплайна.)

(`_document_state` / `_pair_payload` — локальные хелперы файла теста,
собирающие минимальный валидный payload с `task`/`config`/`checklists`/
`budget_used`/`anchor_id`; в них же лежит `documents` нужной формы.)

- [ ] **Шаг 6: прогнать, убедиться что падает**

Run: `uv run pytest tests/contracts/test_pipeline_kind.py -q`
Ожидание: FAIL — коллекция чужого вида принимается, схема не проверяется.

- [ ] **Шаг 7: реализация в `PipelineState`**

```python
    documents: Documents
    spec_sessions: list[SessionRecord] = Field(default_factory=list)
    pair_sessions: list[SessionRecord] = Field(default_factory=list)
    doc_sessions: list[SessionRecord] = Field(default_factory=list)

    @property
    def kind(self) -> PipelineKind:
        """Вид читается из дискриминатора и больше ниоткуда (§4.2).

        Второе поле верхнего уровня пришлось бы сверять с этим при каждом
        чтении, а расхождение двух записей об одном факте нечем разрешить.
        """
        return PipelineKind(self.documents.kind)

    @model_validator(mode="after")
    def _validate_kind_consistency(self) -> "PipelineState":
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
```

`PipelineArtifactBase.schema_` расширяется до
`Literal["disputatio/pipeline/v1", "disputatio/pipeline/v2"]`, подстановка в
`__init__` даёт `SCHEMA_PIPELINE_V2` (пишем всегда v2, §4.2), а чтение v1
чинится нормализацией **до** дискриминации:

```python
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
        data = {**data, "documents": {**documents, "kind": "pair"}}
        data["schema"] = SCHEMA_PIPELINE_V2
        return data
```

Подъём тега прямо здесь — не побочный эффект: прочитанный v1-манифест уже
представлен в памяти v2-формой, и оставить ему прежний тег значило бы записать
обратно файл, чья форма не совпадает с объявленной.

- [ ] **Шаг 8: прогнать — тесты проходят, suite зелёный**

Run: `uv run pytest tests/contracts/test_pipeline_kind.py -q && uv run pytest -q`
Ожидание: PASS. Существующие тесты манифеста продолжают проходить; если
какой-то сравнивает сериализацию с эталоном — обновить эталон **по обоим
полям сразу**: `"schema": "disputatio/pipeline/v2"` и `"kind": "pair"`.
Обновить только одно значит зафиксировать эталоном запрещённое состояние.
Изменение назвать в сообщении коммита.

Отдельным тестом закрепить миграцию целиком, а не по кускам:

```python
def test_v1_fixture_saves_as_v2_and_keeps_everything_else(tmp_path) -> None:
    """Пара переходит на v2 ровно двумя полями и ничем больше."""
    before = json.loads(_V1_FIXTURE.read_text(encoding="utf-8"))
    after = PipelineState.model_validate(before).model_dump(mode="json")
    assert after["schema"] == "disputatio/pipeline/v2"
    assert after["documents"]["kind"] == "pair"
    stripped = {**after, "schema": before["schema"]}
    stripped["documents"] = {
        k: v for k, v in after["documents"].items() if k != "kind"
    }
    assert stripped == before
```

- [ ] **Шаг 9: коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/disputatio/contracts/ src/disputatio/runtime/ \
        src/disputatio/cli.py tests/contracts/ tests/runtime/test_pipeline_export.py
git commit -m "feat(contracts): вид пайплайна как дискриминатор documents"
```

---

### Задача 2: contracts — findings-item как роль, разрешённый чеклист

**Файлы:**
- Modify: `src/disputatio/contracts/checklists_catalog.py`
- Modify: `src/disputatio/contracts/validation.py:160-245`
- Modify: `src/disputatio/contracts/__init__.py`
- Modify: `src/disputatio/runtime/steps.py` — единственный боевой вызов
  `validate_doc_review` **и тип `DocSessionSpec.checklist`**
  (`Mapping[str, str]` → `ResolvedChecklist`, `steps.py:130`); `contour`
  расширяется до `str`
- Modify: `src/disputatio/runtime/composition.py:493` — собирает
  `DocSessionSpec.checklist`; сейчас делает `dict(config.checklists[contour])`
- Modify: `tests/contracts/test_doc_review_validation.py` — **18 существующих
  вызовов** `validate_doc_review`; смена сигнатуры роняет их все
- Modify: `tests/contracts/test_init.py:68` — фиксирует состав `__all__`,
  где лежит переименовываемая константа V8
- Test: `tests/contracts/test_checklist_role.py`

**Интерфейсы (потребляет):** ничего из задачи 1.

**Тип тянется сквозным за один заход, а не оседает на полпути.** Цепочка,
по которой чеклист доходит до ревьюера, состоит из четырёх звеньев:
`PipelineConfig.checklists` → `composition.py:493` → `DocSessionSpec.checklist`
→ промпт и `validate_doc_review`. Задача 2 переводит на `ResolvedChecklist`
три последних звена, собирая объект в `composition.py:493` из вендоренного
каталога и текстов конфига (сам конфиг здесь ещё отдаёт `Mapping[str, str]`).
Задача 3 переводит первое звено и упрощает `composition.py:493` до передачи
уже разрешённого объекта. Так каждая из двух задач заканчивается зелёным
suite; оставить `ResolvedChecklist` жить только внутри валидатора значило бы
собирать его дважды и разойтись между сборками.

**Интерфейсы (производит):**

```python
@dataclass(frozen=True, slots=True)
class ResolvedChecklist:
    """Действующий чеклист контура: состав, порядок, роль (§5.3)."""

    order: tuple[str, ...]
    texts: Mapping[str, str]
    findings_item: str | None

FINDINGS_ITEM_BY_CONTOUR: Final[dict[str, str | None]] = {
    "spec": "S1",
    "pair": None,
}

def validate_doc_review(
    review: Review,
    *,
    contour: str,
    checklist: ResolvedChecklist,
    verification: VerificationReport,
) -> list[str]: ...
```

**Почему роль, а не литерал.** V8 в редакции v0.1 искал пункт `"S1"`. Для
контура `pair` такого id нет вовсе — `PAIR_CHECKLIST` это P1–P5, — значит
правило не срабатывало ни разу, и это нигде не было заявлено (§5.2, §5.3).
Роль типа `str | None` делает пустоту записанной, а не выведенной из
ненайденного имени.

- [ ] **Шаг 1: red-тест — роль и её пустота**

```python
# tests/contracts/test_checklist_role.py
from disputatio.contracts.checklists_catalog import FINDINGS_ITEM_BY_CONTOUR


def test_pair_findings_item_is_explicitly_absent() -> None:
    """Пустота роли у pair — записанное утверждение, а не ненайденный литерал."""
    assert "pair" in FINDINGS_ITEM_BY_CONTOUR
    assert FINDINGS_ITEM_BY_CONTOUR["pair"] is None


def test_spec_findings_item_is_s1() -> None:
    assert FINDINGS_ITEM_BY_CONTOUR["spec"] == "S1"
```

- [ ] **Шаг 2: прогнать, убедиться что падает**

Run: `uv run pytest tests/contracts/test_checklist_role.py -q`
Ожидание: FAIL — `ImportError: cannot import name 'FINDINGS_ITEM_BY_CONTOUR'`.

- [ ] **Шаг 3: реализация каталога**

```python
FINDINGS_ITEM_BY_CONTOUR: Final[dict[str, str | None]] = {
    "spec": "S1",
    # У pair подходящего пункта НЕТ: P1-P5 говорят о трассируемости,
    # отсутствии новых решений, порядке реализации, доказательности тестов
    # и выполнимости команд — ни одно не эквивалентно «находок нет». Эту
    # работу у пары делает V7. Пустота записана, а не выведена из того,
    # что литерал не нашёлся (§5.3 SPEC-002).
    "pair": None,
}
```

- [ ] **Шаг 4: прогнать — проходит**

Run: `uv run pytest tests/contracts/test_checklist_role.py -q`
Ожидание: PASS.

- [ ] **Шаг 5: red-тест — V8 по роли и V1 по разрешённому набору**

```python
def test_v8_fires_on_role_item_not_on_literal_s1() -> None:
    """Правило проверяет назначенный пункт, а имени S1 не знает."""
    checklist = ResolvedChecklist(
        order=("B1", "B3"),
        texts={"B1": "…", "B3": "нет blocker/major-находок"},
        findings_item="B3",
    )
    review = _review(
        verdict=Verdict.REQUEST_CHANGES,
        issues=[_issue("R1-1", Severity.BLOCKER)],
        checklist=[
            _item("B1", "pass"),
            _item("B3", "pass"),
        ],
    )
    assert REASON_CHECKLIST_CONTRADICTS_ISSUES in validate_doc_review(
        review, contour="doc", checklist=checklist,
        verification=_verification_ok(),
    )


def test_v8_silent_for_contour_without_role() -> None:
    """Пустая роль — законное бездействие, и оно проверено как объявленное."""
    checklist = ResolvedChecklist(
        order=("P1",), texts={"P1": "…"}, findings_item=None
    )
    review = _review(
        verdict=Verdict.REQUEST_CHANGES,
        issues=[_issue("R1-1", Severity.BLOCKER, defect_class="execution")],
        checklist=[_item("P1", "pass")],
    )
    assert REASON_CHECKLIST_CONTRADICTS_ISSUES not in validate_doc_review(
        review, contour="pair", checklist=checklist,
        verification=_verification_ok(),
    )


def test_v1_uses_resolved_set_not_global_catalog() -> None:
    checklist = ResolvedChecklist(
        order=("B1",), texts={"B1": "…"}, findings_item="B1"
    )
    review = _review(
        verdict=Verdict.APPROVE, issues=[], checklist=[_item("B1", "pass")]
    )
    assert validate_doc_review(
        review, contour="doc", checklist=checklist,
        verification=_verification_ok(),
    ) == []
```

- [ ] **Шаг 6: прогнать, убедиться что падает**

Run: `uv run pytest tests/contracts/test_checklist_role.py -q`
Ожидание: FAIL — `validate_doc_review() got an unexpected keyword argument
'checklist'`.

- [ ] **Шаг 7: реализация валидатора**

Сигнатуру `validate_doc_review` расширить параметром `checklist:
ResolvedChecklist`, `contour` расширить до `str`. Заменить две строки:

```python
    expected_ids = set(checklist.order)      # было: CHECKLIST_BY_CONTOUR[contour]
```

```python
    role_id = checklist.findings_item
    if role_id is not None:
        role = next((item for item in items if item.id == role_id), None)
        if role is not None and role.status == "pass" and substantive_issues:
            errors.append(REASON_CHECKLIST_CONTRADICTS_ISSUES)
```

**Переименование константы V8 — 6 файлов, 14 мест** (точный список:
`grep -rn "PASS_CONTRADICTS_S1" src tests`). Меняется и имя, и
машинно-читаемое значение, потому что после перехода на роль лгут оба:

```python
# было
REASON_CHECKLIST_PASS_CONTRADICTS_S1 = "checklist_pass_contradicts_s1"
# стало
REASON_CHECKLIST_CONTRADICTS_ISSUES = "checklist_pass_contradicts_issues"
```

Места — четыре файла, 14 вхождений: `contracts/validation.py` (2),
`contracts/__init__.py` (2: импорт и `__all__`),
`tests/contracts/test_init.py:68` (1),
`tests/contracts/test_doc_review_validation.py` (9).
Старое имя удалить, алиаса не оставлять: два имени одного кода разошлись бы в
сообщениях. Значение уходит в текст retry ревьюеру — то есть код, называющий
`s1` там, где контур `doc` про `S1` не слышал, вводил бы в заблуждение агента,
а не только читателя.

Правило V5 оставить условием `if contour == "pair"` — оно и так неприменимо
к `spec` и `doc` (§5.2).

- [ ] **Шаг 8: прогнать — проходит, suite зелёный**

Run: `uv run pytest tests/contracts -q && uv run pytest -q`
Ожидание: PASS — но только после обновления **всех** существующих вызовов, а их
19 (`grep -rn "validate_doc_review(" src tests`): один боевой в
`runtime/steps.py` (собрать `ResolvedChecklist` из доступного там
`spec.checklist`) и 18 в `tests/contracts/test_doc_review_validation.py`.

Чтобы правка 18 тестов была механической, а не творческой, завести в этом же
файле хелпер и звать его вместо литералов:

```python
def _resolved(contour: str) -> ResolvedChecklist:
    """Разрешённый чеклист встроенного контура — вендоренный состав."""
    order = CHECKLIST_BY_CONTOUR[contour]
    return ResolvedChecklist(
        order=order,
        texts={item_id: CHECKLIST_TEXT[item_id] for item_id in order},
        findings_item=FINDINGS_ITEM_BY_CONTOUR[contour],
    )
```

**Один из 18 меняет ожидание, а не только вызов.** Тест, утверждающий, что
`S1: pass` при blocker/major отвергается, для контура `pair` проходил лишь
потому, что искал литерал `S1` в pair-чеклисте и не находил. Разобраться, какой
именно это тест, и привести его к объявленному поведению: у `pair` роль пуста,
V8 бездействует, работу делает V7.

- [ ] **Шаг 9: коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/disputatio/contracts/ src/disputatio/runtime/steps.py \
        src/disputatio/runtime/composition.py tests/contracts/
git commit -m "feat(contracts): V8 привязан к роли findings-item, а не к S1"
```

---

### Задача 3: runtime — конфиг двух взаимоисключающих форм

**Файлы:**
- Modify: `src/disputatio/runtime/pipeline_config.py`
- Modify: `src/disputatio/runtime/composition.py:493` — перестаёт собирать
  `ResolvedChecklist` и передаёт уже разрешённый объект конфига
- Modify: `tests/runtime/test_pipeline_config.py` — **7 строк индексируют
  `config.checklists[...][...]` как словарь** (напр. `:127`, `:272`); после
  смены типа обращение идёт через `.texts[...]`
- Modify: **все читатели `config.spec_path`/`.plan_path`** — поля становятся
  `Path | None`, и `.as_posix()` на них немедленно краснит `pyrefly`:
  `pipeline_runner.py:410,411,1184,1185`, `composition.py:596,599,621,623`,
  `pipeline_adopt.py:126,128,158`
- Modify: **четыре места, конструирующие `PipelineConfig` напрямую** —
  `kind` обязателен и дефолта не имеет: `tests/runtime/_pipeline_stand.py:337`,
  `tests/runtime/test_pipeline_runner.py:413`,
  `tests/runtime/test_pipeline_adopt.py:753`,
  `tests/runtime/test_pipeline_config.py:315`
- Test: `tests/runtime/test_pipeline_config_kinds.py`

**Интерфейсы (потребляет):** `PipelineKind`, `ResolvedChecklist` (задачи 1–2).

**Интерфейсы (производит):**

```python
@dataclass(frozen=True, slots=True)
class PipelineConfig:
    kind: PipelineKind
    spec_path: Path | None = None
    plan_path: Path | None = None
    document_path: Path | None = None
    # остальные поля без изменений
    checklists: Mapping[str, ResolvedChecklist] = ...

    def documents(self) -> tuple[Path, ...]: ...
    def contour_documents(self, contour: str) -> tuple[str, ...]: ...
    def scope_paths(self, contour: str) -> tuple[str, ...]: ...
```

Опциональность трёх путей здесь — не отступление от P10: `PipelineConfig` это
**разобранный конфиг**, а он обязан уметь представить обе формы. Невыразимость
чужой формы держит манифест (задача 1) и fail-closed разбор ниже; тип
`PipelineConfig` строит его.

**Но сырые опциональные поля наружу не выходят — их закрывают три аксессора,
и читатели переходят на них в этой же задаче.** Причина та же, что в задаче 1:
как только `spec_path` становится `Path | None`, одиннадцать существующих
`.as_posix()` краснят `pyrefly`, а он обязателен после каждой задачи. Отложить
их до задач 5 и 6 нельзя.

| Читатель | Было | Стало |
|---|---|---|
| `pipeline_runner.py:410-411` | `config.spec_path.as_posix()` ×2 | `_documents()` (шаг задачи 5) |
| `pipeline_runner.py:1184-1185` | две строки TOML | `config.documents()` |
| `composition.py:596-599` | `_doc_paths` тернарником | `config.contour_documents(contour)` |
| `composition.py:621-623` | `allowed` тернарником | `config.scope_paths(contour)` |
| `pipeline_adopt.py:126,128,158` | `allowed` из пары | `config.scope_paths(contour)` |

Сигнатуры аксессоров объявлены выше и в задачах 5–6 уже не меняются: те
задачи меняют семантику вида, а не способ добраться до путей.

- [ ] **Шаг 1: red-тест — XOR форм и текст отказа**

```python
# tests/runtime/test_pipeline_config_kinds.py
import pytest

from disputatio.contracts.pipeline import PipelineKind
from disputatio.runtime.errors import ConfigError
from disputatio.runtime.pipeline_config import load_pipeline_config


def _write(tmp_path, body: str):
    path = tmp_path / "disputatio.toml"
    path.write_text(body, encoding="utf-8")
    return path


_AGENTS = """
[agents.author]
adapter = "fake"
[agents.reviewer]
adapter = "fake"
[limits]
max_rounds = 3
"""


def test_document_form_yields_document_kind(tmp_path) -> None:
    path = _write(tmp_path, """
[pipeline]
document_path = "docs/charter.md"
[pipeline.checklists.doc]
findings_item = "B1"
[pipeline.checklists.doc.items]
B1 = "нет blocker/major-находок"
""" + _AGENTS)
    config = load_pipeline_config(path)
    assert config.kind is PipelineKind.DOCUMENT
    assert config.document_path.as_posix() == "docs/charter.md"


_DOC_CHECKLIST = """
[pipeline.checklists.doc]
findings_item = "B1"
[pipeline.checklists.doc.items]
B1 = "нет находок"
"""


@pytest.mark.parametrize(
    "body",
    [
        # смешанная форма
        '[pipeline]\ndocument_path = "d.md"\nspec_path = "s.md"\n',
        # пара наполовину
        '[pipeline]\nspec_path = "s.md"\n',
        # ни одной формы
        "[pipeline]\n",
        # ключ чужой формы при document_path
        '[pipeline]\ndocument_path = "d.md"\n'
        "max_architectural_returns = 2\n" + _DOC_CHECKLIST,
    ],
    ids=["mixed", "half-pair", "empty", "foreign-key"],
)
def test_every_form_refusal_names_both_schemas(tmp_path, body: str) -> None:
    """C3 и §10: КАЖДЫЙ отказ формы перечисляет обе допустимые схемы.

    Проверять один лишь факт `raises` мало: диагностическая часть C3 —
    обязательное требование, и без утверждения о тексте она регрессирует
    молча, оставляя тест зелёным.
    """
    with pytest.raises(ConfigError) as excinfo:
        load_pipeline_config(_write(tmp_path, body + _AGENTS))
    message = str(excinfo.value)
    assert "document_path" in message
    assert "spec_path" in message
    assert "plan_path" in message


def test_foreign_key_refusal_also_names_the_key(tmp_path) -> None:
    """Обе схемы — не вместо причины отказа, а вместе с ней."""
    body = (
        '[pipeline]\ndocument_path = "d.md"\n'
        "max_architectural_returns = 2\n" + _DOC_CHECKLIST
    )
    with pytest.raises(ConfigError, match="max_architectural_returns"):
        load_pipeline_config(_write(tmp_path, body + _AGENTS))
```

- [ ] **Шаг 2: прогнать, убедиться что падает**

Run: `uv run pytest tests/runtime/test_pipeline_config_kinds.py -q`
Ожидание: FAIL — `document_path` неизвестен, `spec_path` обязателен.

- [ ] **Шаг 3: реализация разбора формы**

```python
_PAIR_FORM = (
    "  [pipeline]\n"
    '  spec_path = "docs/specs/…-design.md"\n'
    '  plan_path = "docs/plans/…-plan.md"'
)
_DOCUMENT_FORM = (
    "  [pipeline]\n"
    '  document_path = "docs/charter.md"\n'
    "  [pipeline.checklists.doc]\n"
    '  findings_item = "B3"\n'
    "  [pipeline.checklists.doc.items]\n"
    '  B3 = "нет blocker/major-находок"'
)


def _both_forms(problem: str) -> str:
    """Текст отказа обязан назвать ОБЕ схемы, а не только нарушенную (C3)."""
    return (
        f"{problem}\n\nСекция [pipeline] существует в двух "
        f"взаимоисключающих формах:\n\nпара «спека + план»:\n{_PAIR_FORM}\n\n"
        f"одиночный документ:\n{_DOCUMENT_FORM}"
    )


def _resolve_kind(table: Mapping[str, Any]) -> PipelineKind:
    has_document = "document_path" in table
    has_spec = "spec_path" in table
    has_plan = "plan_path" in table
    if has_document and (has_spec or has_plan):
        raise ConfigError(_both_forms(
            "[pipeline] смешивает формы: document_path задан вместе с путями пары"
        ))
    if has_document:
        return PipelineKind.DOCUMENT
    if has_spec and has_plan:
        return PipelineKind.PAIR
    if has_spec or has_plan:
        raise ConfigError(_both_forms(
            "[pipeline] задаёт пару наполовину: нужны оба пути"
        ))
    raise ConfigError(_both_forms("[pipeline] не задаёт ни одной из форм"))
```

Отказ на `max_architectural_returns` при `DOCUMENT` — сразу после
`_resolve_kind`, и он тоже проходит через `_both_forms`: оператор написал ключ
из формы пары, значит показать ему обе формы — прямой ответ на его ошибку, а не
шум (§3.2, C3):

```python
    if kind is PipelineKind.DOCUMENT and "max_architectural_returns" in table:
        raise ConfigError(_both_forms(
            "max_architectural_returns не применим к виду document: возвратов "
            "у него нет. Ключ отвергается, а не игнорируется — молча "
            "проигнорированная настройка оператора хуже отказа"
        ))
```

- [ ] **Шаг 4: прогнать — проходит**

Run: `uv run pytest tests/runtime/test_pipeline_config_kinds.py -q`
Ожидание: PASS.

- [ ] **Шаг 5: red-тест — операторский чеклист и его отказы**

```python
def test_doc_checklist_order_follows_declaration(tmp_path) -> None:
    """Порядок объявления, а не алфавит: промпт обязан быть воспроизводим."""
    path = _write(tmp_path, """
[pipeline]
document_path = "docs/charter.md"
[pipeline.checklists.doc]
findings_item = "B3"
[pipeline.checklists.doc.items]
B3 = "нет blocker/major-находок"
B1 = "каждый BEH-NN несёт traces:"
""" + _AGENTS)
    checklist = load_pipeline_config(path).checklists["doc"]
    assert checklist.order == ("B3", "B1")
    assert checklist.findings_item == "B3"


@pytest.mark.parametrize(
    "block, expected",
    [
        ('[pipeline.checklists.doc]\n[pipeline.checklists.doc.items]\n',
         "findings_item"),
        ('[pipeline.checklists.doc]\nfindings_item = "ZZ"\n'
         '[pipeline.checklists.doc.items]\nB1 = "x"\n', "ZZ"),
        ('[pipeline.checklists.doc]\nfindings_item = "B1"\n'
         '[pipeline.checklists.doc.items]\n', "пуст"),
    ],
)
def test_doc_checklist_failures(tmp_path, block: str, expected: str) -> None:
    path = _write(
        tmp_path,
        '[pipeline]\ndocument_path = "docs/charter.md"\n' + block + _AGENTS,
    )
    with pytest.raises(ConfigError, match=expected):
        load_pipeline_config(path)


def test_pair_checklist_form_unchanged(tmp_path) -> None:
    """Форма пары не менялась: плоское {id = текст}, состав фиксирован."""
    path = _write(tmp_path, """
[pipeline]
spec_path = "docs/spec.md"
plan_path = "docs/plan.md"
[pipeline.checklists.spec]
S1 = "переписанный текст"
""" + _AGENTS)
    checklist = load_pipeline_config(path).checklists["spec"]
    assert checklist.order == ("S1", "S2", "S3", "S4", "S5")
    assert checklist.texts["S1"] == "переписанный текст"
    assert checklist.findings_item == "S1"
```

- [ ] **Шаг 6: прогнать, убедиться что падает**

Run: `uv run pytest tests/runtime/test_pipeline_config_kinds.py -q`
Ожидание: FAIL — `checklists` отдаёт `Mapping[str, Mapping[str, str]]`.

- [ ] **Шаг 7: реализация чеклистов**

`_checklists` расщепляется по происхождению набора:

```python
def _checklists(
    value: Any, kind: PipelineKind
) -> dict[str, ResolvedChecklist]:
    """Два происхождения набора, две формы таблицы (§5.3 SPEC-002).

    Для встроенных контуров конфиг переписывает ТЕКСТЫ вендоренного набора;
    для операторского `doc` объявляет набор целиком вместе с ролью. Разная
    форма отражает разную природу: критерий сходимости чартера знает автор
    документа, а не это репо.
    """
```

Для `PipelineKind.PAIR` — существующая логика merge поверх дефолта, обёрнутая
в `ResolvedChecklist(order=CHECKLIST_BY_CONTOUR[c], texts=merged[c],
findings_item=FINDINGS_ITEM_BY_CONTOUR[c])`.

Для `PipelineKind.DOCUMENT` — новая ветка:

```python
    table = value.get("doc")
    if not isinstance(table, Mapping):
        raise ConfigError(
            "[pipeline.checklists.doc] обязательна для вида document: "
            "вендоренного набора у операторского контура нет"
        )
    items = table.get("items")
    if not isinstance(items, Mapping) or not items:
        raise ConfigError(
            "[pipeline.checklists.doc.items] пуст: критерий сходимости "
            "документа обязан быть объявлен"
        )
    role = table.get("findings_item")
    if not isinstance(role, str):
        raise ConfigError(
            "[pipeline.checklists.doc] обязана назначить findings_item — "
            "пункт со смыслом «нет blocker/major-находок». Без него правило "
            "V8 стало бы тихим no-op'ом через конфигурацию (§5.3)"
        )
    if role not in items:
        raise ConfigError(
            f"[pipeline.checklists.doc] findings_item = {role!r} не назван "
            f"среди items: {sorted(items)}"
        )
```

Порядок — `tuple(items)`: `tomllib` сохраняет порядок объявления файла, а сам
файл снапшотится при `run`, поэтому порядок детерминирован на всю жизнь
пайплайна (§5.3).

- [ ] **Шаг 8: прогнать — проходит, suite зелёный**

Run: `uv run pytest tests/runtime -q && uv run pytest -q`
Ожидание: PASS.

- [ ] **Шаг 9: коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/disputatio/runtime/ tests/runtime/
git commit -m "feat(pipeline): две формы [pipeline], fail-closed разбор вида"
```

---

### Задача 4: context — промпты контура `doc`

**Файлы:**
- Modify: `src/disputatio/context/doc_author.py:40-90`
- Modify: `src/disputatio/context/doc_reviewer.py:60-190`
- Modify: `src/disputatio/runtime/steps.py` — два боевых вызова сборщиков
- Modify: `tests/context/test_doc_prompts.py` — **18 существующих вызовов**;
  смена типа `checklist` роняет их все. Один из них проверяет сигнатуру
  через `inspect.signature` (`test_doc_prompts.py:130`) — его ожидание
  придётся обновить вместе с сигнатурой, а не «починить» подгонкой
- Test: `tests/context/test_doc_prompts_document_kind.py`

**Интерфейсы (потребляет):** `ResolvedChecklist` (задача 2).

**Интерфейсы (производит):**

```python
def build_doc_author_prompt(
    *, contour: str, task_text: str, doc_paths: Sequence[str],
    directive: str | None, adopted_findings: Sequence[Issue] = (),
) -> str: ...

def build_doc_reviewer_prompt(
    *, contour: str, doc_texts: Mapping[str, str],
    verification: VerificationReport, checklist: ResolvedChecklist,
) -> str: ...
```

- [ ] **Шаг 1: red-тест — промпты контура doc**

```python
def test_doc_author_prompt_has_own_intro() -> None:
    prompt = build_doc_author_prompt(
        contour="doc", task_text="написать чартер",
        doc_paths=("docs/charter.md",), directive=None,
    )
    assert "docs/charter.md" in prompt
    assert "спек" not in prompt.lower().split("документ")[0][:200]


def test_doc_reviewer_prompt_orders_by_resolved_checklist() -> None:
    checklist = ResolvedChecklist(
        order=("B3", "B1"), texts={"B3": "третий", "B1": "первый"},
        findings_item="B3",
    )
    prompt = build_doc_reviewer_prompt(
        contour="doc", doc_texts={"docs/charter.md": "# Ч"},
        verification=_verification_ok(), checklist=checklist,
    )
    assert prompt.index("третий") < prompt.index("первый")


def test_doc_reviewer_prompt_has_no_defect_class_note() -> None:
    """Возвращаться некуда — требование класса дефекта было бы ложью."""
    prompt = build_doc_reviewer_prompt(
        contour="doc", doc_texts={"docs/charter.md": "# Ч"},
        verification=_verification_ok(),
        checklist=ResolvedChecklist(("B1",), {"B1": "x"}, "B1"),
    )
    assert "defect_class" not in prompt


def test_reviewer_prompt_is_byte_reproducible() -> None:
    args = dict(
        contour="doc", doc_texts={"docs/charter.md": "# Ч"},
        verification=_verification_ok(),
        checklist=ResolvedChecklist(("B3", "B1"), {"B3": "a", "B1": "b"}, "B3"),
    )
    assert build_doc_reviewer_prompt(**args) == build_doc_reviewer_prompt(**args)
```

- [ ] **Шаг 2: прогнать, убедиться что падает**

Run: `uv run pytest tests/context/test_doc_prompts_document_kind.py -q`
Ожидание: FAIL — `KeyError: 'doc'` в `_INTRO_BY_CONTOUR`.

- [ ] **Шаг 3: реализация**

В `doc_author.py` добавить интро контура `doc`:

```python
    "doc": (
        "Ты автор документа. Задача — довести единственный документ, "
        "названный ниже, до сходимости по чеклисту ревьюера. Права правки "
        "чего-либо ещё у тебя нет: диф раунда, тронувший другой путь, валит "
        "детерминированный гейт doc-scope (§6 SPEC-002). Источник истины — "
        "файлы рабочей директории, а не история диалога (§6.1 SPEC-001)."
    ),
```

В `doc_reviewer.py` — интро контура `doc`; `_check_checklist_ids` и
`_render_checklist_section` принимают `ResolvedChecklist` и берут состав и
порядок из него, а не из `CHECKLIST_BY_CONTOUR`. Блок `_PAIR_DEFECT_CLASS_NOTE`
остаётся под условием `contour == "pair"` — оно уже такое.

- [ ] **Шаг 4: прогнать — проходит, suite зелёный**

Run: `uv run pytest tests/context -q && uv run pytest -q`
Ожидание: PASS.

- [ ] **Шаг 5: коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/disputatio/context/ src/disputatio/runtime/steps.py tests/context/
git commit -m "feat(context): промпты контура doc, чеклист приходит разрешённым"
```

---

### Задача 5: runtime — runner под вид

**Файлы:**
- Modify: `src/disputatio/runtime/pipeline_runner.py` (`__init__`, `run`,
  `_do_run_session`, `_do_finish_session`, `_enter_export`, `_start_pair`,
  `_records`, `_records_update`, **`_config_snapshot`, `_checklists_snapshot`,
  `active_session`, `recompute_budget`**) — `_entry_hashes` уже переведён на
  `documents.paths()` задачей 1 и здесь не трогается
- Modify: `src/disputatio/runtime/composition.py` (`build_pipeline` — сборка
  таблицы политик)
- Modify: `tests/runtime/_pipeline_stand.py:368`,
  `tests/runtime/test_pipeline_runner.py:429,473` — существующие ТЕСТОВЫЕ
  места, где `PipelineRunner` конструируется напрямую (четвёртое, боевое —
  `composition.py:510`, выше)
- Test: `tests/runtime/test_pipeline_runner_document.py`

**Интерфейсы (потребляет):** `CONTOURS_BY_KIND`, `TERMINAL_CONTOUR`,
`ENTRY_PHASE`, `SESSIONS_FIELD_BY_CONTOUR`, `PipelineKind` (задача 1);
`PipelineConfig.kind` (задача 3).

**Интерфейсы (производит):**

```python
class PipelineRunner:
    def __init__(
        self,
        *,
        boundary_policies: Mapping[str, RoundBoundaryPolicy],
        # …существующие параметры без изменений
    ) -> None: ...

    @property
    def boundary_policies(self) -> Mapping[str, RoundBoundaryPolicy]:
        """Таблица политик по контурам, собранная при построении.

        Публично — потому что пустота таблицы у вида document это
        наблюдаемое свойство СБОРКИ, а тест на неё не вправе лезть в
        приватные поля (§10 SPEC-002).
        """
```

`CONTOUR_DOC: Final = "doc"`; `PipelineRunner._enter_export(state,
records_update, finished, *, from_phase, reason)`.

**Почему таблица, а не объект и не флаг.** Сегодня политика создаётся ВНУТРИ
runner'а — `pipeline_runner.py:581`:

```python
            policy = ArchitecturalDefectPolicy() if contour == CONTOUR_PAIR else None
```

Пока строка выглядит так, P10 невыполним: механика вида `pair` физически
присутствует в каждом пайплайне и отделена от работы одним условием. Строка
удаляется; политику выбирает `build_pipeline` и отдаёт таблицей по контурам
(§7.1). У вида `pair` в таблице одна запись, у вида `document` она пуста —
объекта политики в таком пайплайне не существует. Runner делает
`self._boundary_policies.get(contour)`; ветвления по виду у него нет.

**Параметр обязателен и дефолта не имеет.** `PipelineRunner` конструируется в
**четырёх** местах (`grep -rn "PipelineRunner(" src tests`), и все четыре
закрывает эта задача — иначе после её коммита либо падает `build_pipeline`,
либо правка боевого файла протекает через границу в чужую задачу:

| Место | Что передать |
|---|---|
| `src/disputatio/runtime/composition.py:510` | собранную здесь же таблицу (боевой путь) |
| `tests/runtime/_pipeline_stand.py:368` | `{CONTOUR_PAIR: ArchitecturalDefectPolicy()}` |
| `tests/runtime/test_pipeline_runner.py:429` | то же |
| `tests/runtime/test_pipeline_runner.py:473` | то же |
Дефолт `{}` был бы худшим из решений: suite позеленел бы молча, а pair-runner
потерял бы политику — то есть P6 (приоритет архитектурного возврата) перестал
бы исполняться, и заметил бы это только живой прогон. `TypeError` на
забытом аргументе честнее.

- [ ] **Шаг 1: red-тест — старт и терминал вида document**

```python
def test_run_seeds_doc_loop_and_first_doc_revision(document_stand) -> None:
    state = document_stand.runner.run("charter", "написать чартер")
    assert state.transitions[0].to is PipelinePhase.DOC_LOOP
    assert state.doc_sessions[0].session_id == "doc-r1"
    assert state.spec_sessions == [] and state.pair_sessions == []


def test_converged_doc_session_goes_straight_to_exporting(document_stand) -> None:
    """Сходимость единственного контура терминальна — второго контура нет."""
    state = document_stand.run_until_converged()
    edges = [(t.from_, t.to, t.reason) for t in state.transitions]
    assert (
        PipelinePhase.DOC_LOOP,
        PipelinePhase.EXPORTING,
        TransitionReason.DOCUMENT_CONVERGED,
    ) in edges
    assert not any(t.to is PipelinePhase.PAIR_LOOP for t in state.transitions)


def test_document_pipeline_holds_no_boundary_policy(document_stand) -> None:
    """P10 проверяется отсутствием ОБЪЕКТА, а не поведением.

    Тест «drive() ведёт себя как без политики» прошёл бы и у политики,
    всегда отвечающей proceed, то есть не отличил бы «не конструируется»
    от запрещённого «не срабатывает» (§10 SPEC-002).
    """
    assert document_stand.runner.boundary_policies == {}


def test_pair_pipeline_holds_exactly_one_boundary_policy(pair_stand) -> None:
    assert set(pair_stand.runner.boundary_policies) == {"pair"}


def test_architectural_return_still_happens(pair_stand) -> None:
    """Регрессия P6 на обновлённом стенде: политика доехала до runner'а.

    Без неё suite позеленел бы на дефолте `{}`, а возврат по архитектурному
    дефекту молча перестал бы происходить.
    """
    state = pair_stand.run_with_architectural_finding()
    assert (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.SPEC_LOOP,
        TransitionReason.ARCHITECTURAL_DEFECT,
    ) in [(t.from_, t.to, t.reason) for t in state.transitions]


def test_pair_pipeline_edges_unchanged(pair_stand) -> None:
    """Регрессия: вид pair не изменил ни одного ребра."""
    state = pair_stand.run_until_converged()
    edges = [(t.from_, t.to, t.reason) for t in state.transitions]
    assert edges[0] == (
        PipelinePhase.IDLE, PipelinePhase.SPEC_LOOP, TransitionReason.STARTED
    )
    assert (
        PipelinePhase.PAIR_LOOP,
        PipelinePhase.EXPORTING,
        TransitionReason.PAIR_CONVERGED,
    ) in edges
```

`document_stand` — фикстура по образцу существующего
`tests/runtime/_pipeline_stand.py`, параметризованная видом; `pair_stand` —
существующий стенд без изменений.

- [ ] **Шаг 2: прогнать, убедиться что падает**

Run: `uv run pytest tests/runtime/test_pipeline_runner_document.py -q`
Ожидание: FAIL — `run` заводит `spec-r1` независимо от конфига.

- [ ] **Шаг 3: реализация — старт по виду**

В `run()` заменить зашитые константы:

```python
        kind = self._config.kind
        first_contour = CONTOURS_BY_KIND[kind][0]
        entry = ENTRY_PHASE[kind]
        first = NextAction(
            operation_id=f"create-{revision_id(first_contour, 1)}",
            kind="create_session",
            args={"contour": first_contour, "revision": 1},
        )
```

`phase=entry`, `to=entry` в стартовом `Transition`, `documents=` собирается по
виду:

```python
    def _documents(self) -> Documents:
        if self._config.kind is PipelineKind.DOCUMENT:
            assert self._config.document_path is not None
            return SingleDocument(
                kind="document",
                document_path=self._config.document_path.as_posix(),
            )
        assert self._config.spec_path is not None
        assert self._config.plan_path is not None
        return PairDocuments(
            spec_path=self._config.spec_path.as_posix(),
            plan_path=self._config.plan_path.as_posix(),
        )
```

- [ ] **Шаг 3-бис: реализация — политика приходит снаружи**

Удалить строку `pipeline_runner.py:581` и взять политику из таблицы:

```python
        if session is None or not self._is_settled(artifact_root, session, contour):
            self._session_driver(
                artifact_root, session_id, self._boundary_policies.get(contour)
            )
```

`ArchitecturalDefectPolicy` больше не импортируется `pipeline_runner`'ом как
конструируемый объект — её создаёт `build_pipeline`:

```python
    boundary_policies: dict[str, RoundBoundaryPolicy] = (
        {CONTOUR_PAIR: ArchitecturalDefectPolicy()}
        if config.kind is PipelineKind.PAIR
        else {}
    )
    runner = PipelineRunner(
        boundary_policies=boundary_policies,
        # …остальные аргументы `composition.py:510` без изменений
    )
```

- [ ] **Шаг 3-тер: реализация — снапшоты и entry_hashes под вид**

Два метода runner'а обязаны быть переписаны в этой же задаче — иначе артефакт
задачи 3 (`ResolvedChecklist`) потребляется старым типом:

- `_config_snapshot` (`pipeline_runner.py:1184`) зовёт `.as_posix()` у
  `spec_path`/`plan_path`, ставших optional. Рендерить по виду: пара — две
  строки и `max_architectural_returns`, документ — `document_path` без него;
- `_checklists_snapshot` (`pipeline_runner.py:1201`) обращается к значению как
  к `Mapping[str, str]` и **сортирует пункты по id**. Оба факта меняются:
  значение теперь `ResolvedChecklist`, а порядок для операторского контура —
  порядок объявления, потому что он входит в identity чеклиста (§5.3 SPEC-002).

```python
    def _checklists_snapshot(self) -> str:
        lines: list[str] = []
        for contour in sorted(self._config.checklists):
            checklist = self._config.checklists[contour]
            lines.append(f"[{contour}]")
            lines.append(f"findings_item = {_toml_string(checklist.findings_item)}"
                         if checklist.findings_item is not None
                         else "findings_item = false")
            # Встроенные контуры — сортировка по id (состав фиксирован, защищаем
            # хеш от порядка ключей конфига). Операторский — порядок объявления:
            # он ЧАСТЬ чеклиста, и отсортированный снапшот его бы потерял.
            order = (
                checklist.order
                if contour == CONTOUR_DOC
                else tuple(sorted(checklist.order))
            )
            for item_id in order:
                lines.append(f"{item_id} = {_toml_string(checklist.texts[item_id])}")
            lines.append("")
        return "\n".join(lines)
```

- [ ] **Шаг 3-кватер: тест контракта снапшота**

Формат снапшота стал частью критерия сходимости (§5.3), значит его надо
закрепить, а не оставить следствием реализации:

```python
def test_doc_snapshot_keeps_declaration_order(document_stand) -> None:
    """Порядок объявления — часть чеклиста, снапшот обязан его сохранить."""
    snapshot = document_stand.checklists_snapshot(order=("B3", "B1"))
    assert snapshot.index("B3 =") < snapshot.index("B1 =")


def test_reordering_doc_items_changes_hash(document_stand) -> None:
    """Два порядка одних условий — разные чеклисты, значит разные байты."""
    first = document_stand.checklists_snapshot(order=("B3", "B1"))
    second = document_stand.checklists_snapshot(order=("B1", "B3"))
    assert first != second


def test_snapshot_carries_findings_item_for_every_contour(pair_stand) -> None:
    """Роль — часть критерия; пустота pair записана, а не подразумевается."""
    snapshot = pair_stand.checklists_snapshot()
    assert 'findings_item = "S1"' in snapshot
    assert "findings_item = false" in snapshot  # контур pair
```

- [ ] **Шаг 4: реализация — терминал контура и параметризованное ребро**

В `_do_finish_session` заменить `if contour == CONTOUR_PAIR:` на

```python
        if contour == TERMINAL_CONTOUR[state.kind]:
            return self._enter_export(
                state,
                records_update,
                finished,
                from_phase=state.phase,
                reason=_CONVERGED_REASON[state.kind],
            )
        return self._start_pair(state, revision, records_update, finished, action)
```

где

```python
_CONVERGED_REASON: Final[dict[PipelineKind, TransitionReason]] = {
    PipelineKind.PAIR: TransitionReason.PAIR_CONVERGED,
    PipelineKind.DOCUMENT: TransitionReason.DOCUMENT_CONVERGED,
}
```

`_enter_export` принимает `from_phase` и `reason` параметрами вместо зашитых.
`_records`/`_records_update` берут имя коллекции из
`SESSIONS_FIELD_BY_CONTOUR[contour]`; `active_session` и `recompute_budget`
обходят все три коллекции.

- [ ] **Шаг 5: прогнать — проходит, suite зелёный**

Run: `uv run pytest tests/runtime -q && uv run pytest -q`
Ожидание: PASS, включая регрессию вида `pair`.

- [ ] **Шаг 6: коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/disputatio/runtime/pipeline_runner.py \
        src/disputatio/runtime/composition.py tests/runtime/
git commit -m "feat(pipeline): runner ведёт контуры по виду, политика приходит таблицей"
```

---

### Задача 6: runtime — composition, adoption, resume под вид

**Файлы:**
- Modify: `src/disputatio/runtime/composition.py:348-624`
- Modify: `src/disputatio/runtime/pipeline_adopt.py:114-160, 539-570`
- Modify: `src/disputatio/runtime/pipeline_resume.py`
- Modify: `tests/runtime/test_pipeline_adopt.py:759` — прямой вызов
  `compute_scope(git, config, allow_plan=True)` старой формой
- Modify: `tests/runtime/_pipeline_stand.py:389` — конструирует
  `OperatorIntents` без нового обязательного `router`
- Test: `tests/runtime/test_document_composition.py`

**Интерфейсы (потребляет):** всё из задач 1, 3, 5.

- [ ] **Шаг 1: red-тест — политика не конструируется, P0, scope**

```python
def test_document_kind_builds_single_contour_router(tmp_repo) -> None:
    """P10: у документного вида СВОЯ реализация порта, а не общая с флагом."""
    deps = build_pipeline(
        _document_config(tmp_repo), _profile(), tmp_repo, "charter", git=_git()
    )
    assert isinstance(deps.intents.router, SingleContourAdoptionRouter)


def test_pair_kind_builds_pair_router(tmp_repo) -> None:
    deps = build_pipeline(
        _pair_config(tmp_repo), _profile(), tmp_repo, "foo", git=_git()
    )
    assert isinstance(deps.intents.router, PairAdoptionRouter)


def test_resume_with_config_of_other_kind_is_rejected(document_stand) -> None:
    """P0: вид неизменяем; смена конфига — отказ до любой мутации."""
    document_stand.runner.run("charter", "…")
    with pytest.raises(ConfigError, match="вид"):
        document_stand.resume_with(_pair_config())


def test_doc_scope_allows_only_the_document(tmp_repo) -> None:
    config = _document_config(tmp_repo)
    assert config.scope_paths("doc") == ("docs/charter.md",)


def test_adoption_outside_document_is_rejected_entirely(document_stand) -> None:
    document_stand.write_file("README.md", "постороннее")
    with pytest.raises(AdoptionScopeError):
        document_stand.adopt_external()


def test_adoption_of_document_opens_next_revision_without_transition(
    document_stand,
) -> None:
    document_stand.write_file("docs/charter.md", "# правка руками")
    state = document_stand.adopt_external()
    assert state.doc_sessions[-1].session_id == "doc-r2"
    assert all(t.to is not PipelinePhase.SPEC_LOOP for t in state.transitions)
```

- [ ] **Шаг 2: прогнать, убедиться что падает**

Run: `uv run pytest tests/runtime/test_document_composition.py -q`
Ожидание: FAIL — `PipelineDeps` не публикует `intents`, порта `AdoptionRouter`
не существует. (Политику `build_pipeline` сегодня не передаёт вообще — её
создаёт сам runner, `pipeline_runner.py:581`; шов закрывает задача 5.)

- [ ] **Шаг 3: реализация — ветка вида в composition root**

Таблицы политик задача 6 **не касается вовсе**: и её контракт, и её передача в
`PipelineRunner` на `composition.py:510` закрыты задачей 5 целиком. Здесь
меняются только маршрутизация adoption, границы документов и resume.
Скалярной политики в плане нет нигде — второй контракт разошёлся бы с публичным
свойством и P10-тестами.

`_contour_of` расширяется до `str` и возвращает контур как есть (`split_revision`
уже даёт `"doc"`); `_doc_paths` и граница `doc-scope` берутся из
`config.contour_documents(contour)` и `config.scope_paths(contour)` —
знание о формах живёт в конфиге, а не размазано по composition root.

`compute_scope` получает разрешённые пути параметром вместо чтения
`config.spec_path`/`plan_path`:

```python
def compute_scope(
    git: GitOps, *, allowed_paths: Sequence[str]
) -> AdoptionScope: ...
```

Маршрутизация adoption становится портом с двумя реализациями, и выбирает их
`build_pipeline`. Общая функция, возвращающая для документного вида тривиальный
ответ, — это запрещённое «не срабатывает» (P10, §3.1): механика пары
присутствовала бы в документном пайплайне и ждала, пока условие в ней однажды
разойдётся с реальностью.

```python
class AdoptionRouter(Protocol):
    def successor(
        self,
        *,
        scope: AdoptionScope,
        contour: str,
        parked: tuple[str, int] | None,
        returns_exhausted: bool,
    ) -> AdoptionRoute:
        """Контур-преемник и причина pipeline-перехода, если он нужен."""


@dataclass(frozen=True, slots=True)
class AdoptionRoute:
    contour: str
    reason: TransitionReason | None


class PairAdoptionRouter:
    """Текущий `_route` целиком. Stateless: динамические факты приходят
    аргументами вызова, а не в конструктор.

    `parked` и `returns_exhausted` меняются от одного adoption к другому
    внутри жизни одного пайплайна; зашить их при сборке значило бы
    заморозить в объекте состояние, которое к следующему вызову уже ложь.
    """

    def successor(
        self,
        *,
        scope: AdoptionScope,
        contour: str,
        parked: tuple[str, int] | None,
        returns_exhausted: bool,
    ) -> AdoptionRoute: ...


class SingleContourAdoptionRouter:
    """Контур один — преемник им и определён.

    Сигнатуру Protocol'а реализация несёт целиком, но pair-факты игнорирует
    и назвать их в теле не может: их там нет. Отдельный узкий Protocol был
    бы честнее по типам, ценой двух портов у одного шва — выбран общий
    Protocol и явное `del` неиспользуемых входов, как в `validate_doc_review`.
    """

    def successor(
        self,
        *,
        scope: AdoptionScope,
        contour: str,
        parked: tuple[str, int] | None,
        returns_exhausted: bool,
    ) -> AdoptionRoute:
        del scope, parked, returns_exhausted
        return AdoptionRoute(contour=contour, reason=None)
```

`OperatorIntents.__init__` получает `router: AdoptionRouter`; `PipelineDeps`
публикует `intents`, чтобы выбор реализации был наблюдаем тестом сборки.

В `pipeline_resume` перед любой мутацией добавить проверку P0:

```python
    if state.kind is not config.kind:
        raise ConfigError(
            f"пайплайн {slug!r} создан как вид {state.kind.value!r}, а "
            f"поданный конфиг описывает {config.kind.value!r}: вид неизменяем "
            "(P0) — сменить его значило бы объявить накопленную историю "
            "переходов принадлежащей другой механике"
        )
```

и обойти шаг 2 (read-only обнаружение дефекта) для вида `document`: обнаруживать
нечего, парковки нет. Шаг 3 (сверка worktree) обязателен у обоих видов и
пропуск шага 2 его не смягчает.

- [ ] **Шаг 4: прогнать — проходит, suite зелёный**

Run: `uv run pytest tests/runtime -q && uv run pytest -q`
Ожидание: PASS.

- [ ] **Шаг 5: коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/disputatio/runtime/ tests/runtime/
git commit -m "feat(pipeline): composition собирает под вид, adoption и resume знают document"
```

---

### Задача 7: экспорт и CLI

**Файлы:**
- Modify: `src/disputatio/runtime/pipeline_export.py:288-320`
- Modify: `src/disputatio/cli.py:396-412` (`render_status`), `546-660` (парсер)
- Test: `tests/runtime/test_export_document.py`, `tests/cli/test_cli_pipeline_kind.py`

- [ ] **Шаг 1: red-тест — экспорт и статус вида document**

```python
def test_export_titles_the_single_document(document_state) -> None:
    result = export_pipeline(document_state, root=..., converged=True)
    assert result.pr_title == "docs: docs/charter.md\n"


def test_export_body_omits_foreign_contour_sections(document_state) -> None:
    """Пустая секция чужого контура неотличима от потерянной."""
    body = export_pipeline(document_state, root=..., converged=True).pr_body
    assert "Документ: `docs/charter.md`" in body
    assert "pair" not in body.lower()


def test_status_prints_kind_first(document_state, tmp_path) -> None:
    first_line = render_status(document_state, tmp_path / "a.jsonl").splitlines()[0]
    assert first_line == "kind: document"


def test_status_of_pair_prints_kind_too(pair_state, tmp_path) -> None:
    lines = render_status(pair_state, tmp_path / "a.jsonl").splitlines()
    assert lines[0] == "kind: pair"
    assert "documents: docs/spec.md + docs/plan.md" in lines


def test_run_help_shows_both_config_forms(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["pipeline", "run", "--help"])
    out = capsys.readouterr().out
    assert "document_path" in out and "spec_path" in out and "plan_path" in out


def test_pipeline_help_names_both_forms(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["pipeline", "--help"])
    out = capsys.readouterr().out
    assert "одиночн" in out and "пар" in out
```

- [ ] **Шаг 2: прогнать, убедиться что падает**

Run: `uv run pytest tests/runtime/test_export_document.py tests/cli/test_cli_pipeline_kind.py -q`
Ожидание: FAIL — заголовок склеивает два пути, статус начинается с `pipeline:`.

- [ ] **Шаг 3: реализация**

`_pr_title` и `_pr_body` ветвятся по `state.kind`; секции сессий строятся по
`CONTOURS_BY_KIND[state.kind]`, а не по фиксированной паре.

`render_status`: `kind: {state.kind.value}` первой строкой; строка `documents:`
рендерится по виду (`{spec} + {plan}` либо один путь).

Парсер: `epilog` подкоманды `run` несёт обе формы `[pipeline]` целиком
(`argparse.RawDescriptionHelpFormatter`), `help` группы `pipeline` — одну строку
про обе поддерживаемые формы.

- [ ] **Шаг 4: прогнать — проходит, suite зелёный**

Run: `uv run pytest -q`
Ожидание: PASS.

- [ ] **Шаг 5: коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/disputatio/runtime/pipeline_export.py src/disputatio/cli.py tests/
git commit -m "feat(pipeline): экспорт и CLI различают вид, help показывает обе формы"
```

---

### Задача 8: сквозные сценарии и документация

**Файлы:**
- Test: `tests/integration/test_document_pipeline_e2e.py`
- Create: `docs/document-pipeline.md`
- Modify: `README.md` (ссылка на новый документ)

- [ ] **Шаг 1: red-тест — сквозной прогон, эскалация, анти-сикофантия**

```python
def test_document_pipeline_runs_to_done(document_repo) -> None:
    state = run_pipeline(document_repo, slug="charter", task="написать чартер")
    assert state.phase is PipelinePhase.DONE
    result = document_repo / ".disputatio/pipelines/charter/result"
    assert (result / "manifest.json").exists()


def test_document_pipeline_escalation_exports_partial(document_repo) -> None:
    state = run_pipeline(document_repo, slug="charter", task="…", deadlock=True)
    assert state.phase is PipelinePhase.DONE
    manifest = _read_result_manifest(document_repo, "charter")
    assert manifest["converged"] is False
    assert manifest["escalation_reason"] == "session_deadlock"


def test_round_one_approve_is_not_accepted(document_repo) -> None:
    """У вида document это ЕДИНСТВЕННАЯ защита: второго контура нет."""
    state = run_pipeline(document_repo, slug="charter", task="…", approve_at_round=1)
    doc_session = state.doc_sessions[0]
    assert _rounds_of(document_repo, doc_session) >= 2


def test_pre_v02_pair_manifest_resumes(document_repo, pair_manifest_fixture) -> None:
    """Регрессия К2: манифест пары, записанный до v0.2, читается и продолжается."""
    state = resume_pipeline(document_repo, slug="legacy")
    assert state.kind is PipelineKind.PAIR
```

`pair_manifest_fixture` — файл `tests/integration/fixtures/pipeline_v1_pair.json`,
записанный **до** этой ветки (снять с существующего теста экспорта) и не
содержащий `documents.kind`.

- [ ] **Шаг 2: прогнать, убедиться что падает**

Run: `uv run pytest tests/integration/test_document_pipeline_e2e.py -q`
Ожидание: FAIL — фикстур и хелперов нет.

- [ ] **Шаг 3: реализация тестов на скриптованных адаптерах**

Использовать существующие fake-адаптеры (`tests/runtime/_fakes.py`) и стенд
`_pipeline_stand.py`, добавив в стенд параметр вида. Новых механизмов не
изобретать: конвейер уже умеет скриптовать артефакты раундов.

- [ ] **Шаг 4: прогнать — проходит**

Run: `uv run pytest tests/integration -q`
Ожидание: PASS.

- [ ] **Шаг 5: документация — сквозной пример (C5)**

`docs/document-pipeline.md`: конфиг вида `document` целиком → команда
`disp pipeline run --slug charter --config disputatio.toml --task task.md` →
состав `result/` → что делает человек дальше (публикация — его шаг). Пример
обязан быть исполнимым дословно, а не иллюстративным.

- [ ] **Шаг 6: полный прогон и коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q
git add tests/integration/ docs/document-pipeline.md README.md
git commit -m "test(pipeline): сквозные сценарии вида document; документированный пример"
```

---

## Самопроверка плана

**Покрытие спеки.** §1 виды → задача 1; §2 таблица и P0/P10 → задачи 1, 6;
§3.1 C1–C5 → задачи 7, 8; §3.2 две формы → задача 3; §4.1 layout → задача 5
(каталоги создаются существующим кодом по имени ревизии); §4.2 union, коллекции,
две версии схемы → задача 1; §5.1 промпт контура → задача 4; §5.2 V1/V5/V8 →
задача 2; §5.3 чеклисты, роль, порядок → задачи 2, 3; §6 `doc-scope` → задача 6;
§7.1 политика приходит таблицей из composition root → **только задача 5**
(и контракт, и передача в `composition.py:510`); §7.2 таблица терминалов → задача 5;
§7.3 неприменим → задача 6 (ветка); §8.1 resume → задача 6; §8.2 экспорт →
задача 7; §9 пакеты — распределение задач ему следует; §10 тесты — каждый пункт
списка редакции v0.2 закреплён за задачей выше.

**Осознанный пропуск.** Гейт под DSL devtools (`#### BEH-NN`, `traces:`,
`checked_by`) в план не входит: §11 SPEC-002 держит его отдельным doc-гейтом
сверх baseline, он не зависит от вида пайплайна и приедет своим PR.

**Финальный инвентарь меняемых интерфейсов** — каждый со своим владельцем и
полным списком потребителей, сверенным `grep`'ом по `src` и `tests`:

| Интерфейс | Владелец | Потребителей |
|---|---|---|
| `DocumentPaths` → `PairDocuments` | задача 1 | 4 файла (runner, 3 теста) |
| `PipelineState.documents` → union | задача 1 | 4 конструктора **+ 5 читателей полей ветки** |
| `PipelineState.doc_sessions` | задача 1 | аддитивно |
| `validate_doc_review(..., checklist=)` | задача 2 | 19 (1 боевой + 18) |
| `REASON_CHECKLIST_PASS_CONTRADICTS_S1` → … | задача 2 | 4 файла / 14 мест |
| `DocSessionSpec.checklist` → `ResolvedChecklist` | задача 2 | 1 (`composition.py:493`) |
| `PipelineConfig.checklists` → `ResolvedChecklist` | задача 3 | 7 индексаций + composition |
| `PipelineConfig.spec_path`/`plan_path` → `Path \| None` | задача 3 | **11 читателей** |
| `PipelineConfig.kind` (обязателен) | задача 3 | 4 прямых конструктора |
| `build_doc_*_prompt(..., checklist=)` | задача 4 | 20 (2 боевых + 18) |
| `PipelineRunner.__init__(boundary_policies=)` | задача 5 | 4 (1 боевой + 3 теста) |
| `_config_snapshot` / `_checklists_snapshot` | задача 5 | внутренние |
| `compute_scope(git, *, allowed_paths)` | задача 6 | 2 (1 боевой + 1 тест) |
| `OperatorIntents.__init__(router=)` | задача 6 | 2 (composition + стенд) |
| `PipelineDeps.intents` | задача 6 | 1 (composition) |
| `render_status` | задача 7 | 1 (cli), тестов сегодня нет |

**Правило, выведенное из этой таблицы:** расширение или опционализация типа
принадлежит той же задаче, что и миграция его читателей. Иначе задача
заканчивается красным `pyrefly` — и именно так план был устроен до раунда 8
в двух местах сразу (union документов и опциональные пути конфига).

Правило проверки перед каждой задачей — в «Глобальных ограничениях»: `grep`
по имени, сверка со списком файлов, расхождение — дефект плана.

**Согласованность имён.** `ResolvedChecklist` (задача 2) — тот же тип в задачах
3 и 4. `SESSIONS_FIELD_BY_CONTOUR` (задача 1) — единственный источник имени
коллекции в задаче 5. `PipelineKind` — из `contracts.pipeline` во всех задачах.
`compute_scope(git, *, allowed_paths)` (задача 6) — единственная сигнатура.
`boundary_policies: Mapping[str, RoundBoundaryPolicy]` (задача 5) — единственный
контракт политики во всём плане; скалярной формы нет нигде. `AdoptionRouter`
stateless: динамические факты (`parked`, `returns_exhausted`) идут аргументами
`successor()`, а не в конструктор.
