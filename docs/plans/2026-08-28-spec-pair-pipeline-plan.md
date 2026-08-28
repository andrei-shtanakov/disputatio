# План имплементации SPEC-002 — пайплайн полировки пары «спека + план»

> **Для agentic-исполнителей:** REQUIRED SUB-SKILL:
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans — задача за задачей, шаги — чекбоксы `- [ ]`.

**Цель:** автоматизировать два вложенных цикла полировки документов
(spec-контур → pair-контур → экспорт готовой к публикации пары) поверх
протокола раундов SPEC-001, без изменения ядра FSM сессии.

**Архитектура:** пайплайн поверх стандартных resumable-сессий; runner ведёт
контуры через два новых runtime-порта (`RoundBoundaryPolicy`,
`SessionLifecyclePolicy`). Состояние пайплайна — write-ahead манифест
`disputatio/pipeline/v1`; состояние целостности (P9) живёт отдельно — в
анкере вне рабочего дерева, и в манифест не дублируется; doc-артефакты —
схема `disputatio/v2`.

**Стек:** Python 3.12+, pydantic, pytest (anyio), uv, pyrefly, ruff.

**Спека:** `disputatio-SPEC-002-doc-pipeline.md` (converged 2026-08-28,
6 раундов Codex-ревью). План аргументирует от неё; исполнитель читает обе.

## Глобальные ограничения

- Пакетный менеджер — только `uv`; тесты — `uv run pytest -q`.
- После каждой задачи: `uv run ruff format . && uv run ruff check .`,
  `uv run pyrefly check`, полный suite зелёный. Строка ≤ 88.
- `core/deciding.py` и `core/machine.py` **не редактируются ни в одной
  задаче** (SPEC-002 V6/§7.1); правка в них = ошибка декомпозиции плана.
- Существующее поведение develop/analyze-сессий байт-в-байт неизменно:
  все расширения — с default'ами, сохраняющими текущий путь
  (`artifact_root = workspace_root`, политики = no-op/None).
- Владение кодом — по §9 SPEC-002 (инварианты D1): модели/порты →
  `contracts`, файловый I/O → `events`, гейты → `verifier`, промпты →
  `context`, оркестрация/composition → `runtime`.
- Коммит после каждой задачи: `feat(pipeline): …` / `feat(contracts): …`
  по пакету задачи; TDD — red-тест до реализации.

---

### Задача 1: contracts — схема `disputatio/v2` для doc-сессий

**Файлы:**
- Modify: `src/disputatio/contracts/session.py` (Mode), `base.py` (версия
  схемы), `review.py` (Review, Issue)
- Create: `src/disputatio/contracts/checklist.py`
- Test: `tests/contracts/test_schema_v2.py`

**Интерфейсы (производит):**
- `Mode.DOCUMENT = "document"` (только в v2-контексте).
- Evidence — **дискриминированный union двух закрытых форм** (§5.2 SPEC-002
  перечисляет ровно две; одна модель с опциональным `lines` пропускала бы
  `artifact` без строк и `gate` со строками):
  `class ArtifactEvidence(ArtifactChild): kind: Literal["artifact"];
  ref: str; lines: str  # обязателен, формат "N" или "N-M"`,
  `class GateEvidence(ArtifactChild): kind: Literal["gate"]; ref: str`
  (поля `lines` нет — лишнее поле отвергается `extra="forbid"` базовой
  модели), `EvidenceRef = Annotated[ArtifactEvidence | GateEvidence,
  Field(discriminator="kind")]`.
- `class ChecklistItem(ArtifactChild): id: str;
  status: Literal["pass","fail","not_applicable"];
  evidence: list[EvidenceRef]  # min_length=1
  issue_ids: list[str] = []`
- `Review.checklist: list[ChecklistItem] | None = None` (аддитивно),
  `Issue.defect_class: Literal["architectural","execution"] | None = None`.
- Версионирование: `ArtifactBase` получает поддержку тега
  `disputatio/v2`; правило «v2-reader принимает v1» — валидатор принимает
  оба тега, писатель doc-сессии ставит v2, develop/analyze продолжают v1
  (§5.1 SPEC-002).

**Шаги:**
- [ ] Red-тесты: `test_mode_document_exists`,
  `test_v2_reader_accepts_v1_payload` (загрузить фикстуру session.json с
  `"schema": "disputatio/v1"` v2-ридером — ок),
  `test_v1_strict_rejects_document_mode` (v1-тег + mode=document —
  ValidationError), `test_checklist_item_requires_evidence` (пустой
  evidence — ValidationError), `test_defect_class_optional_default_none`,
  `test_artifact_evidence_requires_lines` (artifact без `lines` —
  ValidationError), `test_gate_evidence_rejects_lines` (gate с `lines` —
  ValidationError), `test_evidence_lines_format` (`"34-41"`, `"12"` — ок;
  `"abc"`, `"41-34"` — ValidationError).
- [ ] Запустить, убедиться в падении:
  `uv run pytest tests/contracts/test_schema_v2.py -q`.
- [ ] Минимальная реализация; suite + pyrefly + ruff.
- [ ] Commit:
  `feat(contracts): схема disputatio/v2 — Mode.DOCUMENT, checklist, defect_class`.

Трассируемость: SPEC-002 §5.1, §5.2 (структуры).

---

### Задача 2: contracts — валидация V1–V8 doc-ревью

**Файлы:**
- Modify: `src/disputatio/contracts/validation.py`
- Create: `src/disputatio/contracts/checklists_catalog.py` — вендоренные
  наборы id: `SPEC_CHECKLIST = ("S1","S2","S3","S4","S5")`,
  `PAIR_CHECKLIST = ("P1",…,"P5")` с текстами из §5.3 SPEC-002 и
  provenance-комментарием (spec-authoring.md, редакция 2026-08-26).
- Test: `tests/contracts/test_doc_review_validation.py`

**Интерфейсы (производит):**
- `def validate_doc_review(review: Review, *, contour: Literal["spec","pair"],
  verification: VerificationReport) -> list[str]` — список ошибок
  (пустой = валидно); вызывается поверх существующей валидации §4.4
  SPEC-001, тем же механизмом schema-retry.

**Правила (по одному тесту на каждое, красный → зелёный):**
- [ ] V1: набор id чеклиста — ровно контурный (пропуск/чужой id — ошибка).
- [ ] V2: evidence непуст у каждого пункта (закрыто типом задачи 1 —
  тест-регресс на уровне validate_doc_review).
- [ ] V3: `approve` + любой `fail` — ошибка.
- [ ] V4: `fail` ⇒ `issue_ids` непуст, все id существуют в этом ревью,
  severity ≥ major.
- [ ] V5: pair: каждый blocker/major несёт `defect_class`, иначе ошибка.
- [ ] V7: document: `approve` + наличие blocker/major issue — ошибка.
- [ ] V8: `S1: pass` + blocker/major issue — ошибка.
- [ ] Property-тест (hypothesis не тянем — параметризация): произвольная
  комбинация fail-пунктов никогда не проходит с approve.
- [ ] Commit: `feat(contracts): валидация V1-V8 doc-ревью + вендоренные чеклисты`.

Трассируемость: §5.2 (V1–V8), §5.3.

---

### Задача 3: contracts — семейство `disputatio/pipeline/v1`

**Файлы:**
- Create: `src/disputatio/contracts/pipeline.py`
- Test: `tests/contracts/test_pipeline_state.py`

**Интерфейсы (производит):**
- `PipelinePhase(StrEnum)`: IDLE, SPEC_LOOP, PAIR_LOOP, EXPORTING,
  ESCALATED, DONE, FAILED.
- `TransitionReason(StrEnum)`: started, spec_converged, pair_converged,
  architectural_defect, external_spec_adopt, session_deadlock,
  session_budget_hit, max_architectural_returns, pipeline_budget_hit,
  export_partial, exported, session_failed, invariant_violation.
- `ALLOWED_TRANSITIONS: Mapping[PipelinePhase, frozenset[PipelinePhase]]` —
  закрытая таблица §2; `DONE` отсутствует в ключах-источниках.
- `SessionOutcome(StrEnum)`: converged, escalated, failed,
  architectural_defect, abandoned.
- Модели: `EvidenceLink {session_id, round, finding_id}`,
  `SessionRecord {revision, session_id, path, entry_hashes:
  dict[str, str]  # значение sha256 либо literal "absent"
  , outcome: SessionOutcome | None, superseded_by: str | None}`,
  `Transition {from_, to, reason, evidence: list[EvidenceLink], at}`,
  `OperatorDecision {operation_id, kind: Literal["discard_round",
  "adopt_external"], at, worktree_diff_sha256}`,
  `NextAction {operation_id, kind: Literal["create_session","run_session",
  "finish_session","record_return","adopt_external","discard_round",
  "export"], args: dict, predecessor_operation_id: str | None}`,
  `AppendOnlyEntry {prefix_bytes: int, prefix_sha256: str}`,
  `IntegritySnapshot {session_id, round, operation_id,
  immutable: dict[str, str], append_only: dict[str, AppendOnlyEntry]}` —
  JSON-форма ровно как в §4.2 SPEC-002: неизменяемые файлы — плоское
  `{path: sha256}`, журналы — `{path: {prefix_bytes, prefix_sha256}}`.
  Модель объявляется здесь, но в манифест НЕ входит (§4.2: снапшоты живут
  только в анкере) — её пишет и читает `IntegrityAnchor` задачи 6;
  `PipelineState(ArtifactBase)` — все поля §4.2, включая `anchor_id: str`
  (логическое имя анкера, = `pipeline_id`; физического пути в манифесте
  нет — он машинно-зависим и резолвится из конфига).

**Шаги:**
- [ ] Red-тесты: round-trip сериализации; `test_transition_out_of_table_rejected`
  (валидатор Transition против ALLOWED_TRANSITIONS);
  `test_done_has_no_outgoing`; `test_outcome_immutable_by_convention`
  (модель frozen-подход: outcome задаётся один раз — проверка на уровне
  store в задаче 5, здесь — что поле Optional и enum закрыт);
  `test_entry_hashes_absent_marker`; `test_relative_paths_only`
  (абсолютный путь в path — ValidationError).
- [ ] Реализация; suite/pyrefly/ruff; Commit:
  `feat(contracts): PipelineState — семейство disputatio/pipeline/v1`.

Трассируемость: §2 (таблица, P3), §4.2, §4.3 (enum kind).

---

### Задача 4: contracts — порты пайплайна и границы раунда

**Файлы:**
- Modify: `src/disputatio/contracts/ports.py`
- Test: `tests/contracts/test_pipeline_ports.py`

**Интерфейсы (производит):**
```python
@runtime_checkable
class PipelineStateStore(Protocol):
    def load(self, pipeline_id: str) -> PipelineState: ...
    def save(self, state: PipelineState) -> None: ...

class BoundaryVerdict(StrEnum):
    PROCEED = "proceed"
    PARK = "park"

@runtime_checkable
class RoundBoundaryPolicy(Protocol):
    def after_deciding(self, review: Review) -> BoundaryVerdict: ...

@runtime_checkable
class SessionLifecyclePolicy(Protocol):
    def before_author_turn(self, state: SessionState) -> None: ...
    def after_author_turn(self, state: SessionState) -> None: ...
```

**Шаги:**
- [ ] Red-тест: structural-check фейков через `isinstance` (паттерн ADR-004,
  как у существующих портов).
- [ ] Реализация, suite, Commit:
  `feat(contracts): порты PipelineStateStore, RoundBoundaryPolicy,
  SessionLifecyclePolicy`.

Трассируемость: §7.1, §9 (строка contracts).

---

### Задача 5: events — разделение workspace_root / artifact_root

**Файлы:**
- Modify: `src/disputatio/events/paths.py`, `state_store.py`,
  модуль bootstrap (`events/__init__.py` или где живёт `bootstrap_session`),
  `src/disputatio/runtime/composition.py` (прокладка параметра)
- Test: `tests/events/test_artifact_root.py`

**Интерфейсы (производит):**
- Все функции `paths.*` и конструкторы (`FileStateStore`,
  event sink, bootstrap) принимают `artifact_root: Path`; git-слой
  продолжает получать `workspace_root`. `build_runtime(config, root,
  *, artifact_root: Path | None = None, **overrides)` — `None` ⇒
  `artifact_root = root` (текущее поведение).
- ADR-006 переформулирован в docstring: «один artifact_root — одна сессия».

**Шаги:**
- [ ] Red-тесты: `test_default_artifact_root_equals_workspace`
  (пути идентичны сегодняшним — снапшот-сравнение строк путей);
  `test_two_sessions_separate_artifact_roots_no_collision`
  (две сессии, один workspace: два разных session.json, git-операции — в
  workspace); `test_resume_reads_from_artifact_root`.
- [ ] Реализация (механическая прокладка параметра), полный suite —
  регресс-гарантия default'а. Commit:
  `feat(events): artifact_root отделён от workspace_root (ADR-006 v2)`.

Трассируемость: §4.1 («Разделение…»), P1.

---

### Задача 6: events — FilePipelineStateStore, пути пайплайна, event sink

**Файлы:**
- Create: `src/disputatio/events/pipeline_store.py`,
  `src/disputatio/events/pipeline_paths.py`,
  `src/disputatio/events/pipeline_events.py`,
  `src/disputatio/events/integrity_anchor.py`
- Test: `tests/events/test_pipeline_store.py`

**Интерфейсы (производит):**
- `pipeline_dir(workspace_root, slug)` → `.disputatio/pipelines/<slug>` и
  производные (`sessions_dir`, `adoptions_dir`, `result_dir`,
  `events_path`, `manifest_path`); грамматика slug
  `[a-z0-9][a-z0-9._-]{0,63}` — валидация здесь.
- `FilePipelineStateStore(workspace_root)` — реализация порта; save —
  `atomic_write`; **guard истории**: save отклоняет (`ValueError`) любое
  расхождение с prefix-equality для `transitions`, `operator_decisions`,
  `spec_sessions`, `pair_sessions` — уже записанные элементы обязаны
  совпадать поэлементно, новое допускается только в хвост (§4.2 SPEC-002:
  append-only ≠ «длина не уменьшается»). Разрешённые правки прежнего
  элемента ровно две: `outcome` с `null` на значение однократно (P3) и
  `superseded_by`.
- `PipelineEventType(StrEnum)` — закрытый словарь §4.1 SPEC-002, ровно
  шесть значений: `phase_change`, `session_started`, `session_finished`,
  `return_recorded`, `exported`, `error`.
- `PipelineEventSink` — best-effort/zero-or-more: `emit(event)` с
  `operation_id` в payload; тип события — только из `PipelineEventType`
  (чужая строка → `ValueError`); на открытии — ремонт хвоста (последняя
  строка без `\n` или невалидный JSON → усечение).
- `def read_pipeline_events(path: Path) -> list[PipelineEvent]` — **парный
  читатель**, поставляемый вместе с sink'ом (P8: дедупликация — не
  обязанность гипотетического потребителя): подавляет дубли по
  `(operation_id, type)`, молча пропускает оборванный хвост.
- `class IntegrityAnchor` — append-only журнал P9 вне рабочего дерева:
  `__init__(anchor_root: Path, anchor_id: str)` (файл —
  `<anchor_root>/<anchor_id>.jsonl`), `append(snapshot) -> None` (fsync,
  идемпотентно по `{session_id, round, operation_id}`),
  `last(session_id, round) -> IntegritySnapshot | None`. Живёт в `events`,
  а не в `runtime`: §9 SPEC-002 и D1 отдают файловые append-only writer'ы
  этому пакету; `runtime` держит только политику, использующую анкер.

**Шаги:**
- [ ] Red-тесты: атомарность (temp+rename — нет частичного файла);
  `test_outcome_rewrite_rejected`; `test_transitions_shrink_rejected`;
  `test_transition_edited_in_place_rejected` (та же длина, изменён прежний
  элемент — отказ); `test_operator_decision_edited_rejected`;
  `test_superseded_by_and_first_outcome_allowed`;
  `test_reader_dedupes_by_operation_id`;
  `test_anchor_append_idempotent` (повтор той же записи не удваивает
  строку); `test_anchor_last_returns_latest`;
  `test_superseded_by_set_once` (повторная смена `r2`→`r3` — отказ);
  `test_tail_repair_truncates_partial_line`;
  `test_slug_grammar_rejected`;
  `test_event_vocabulary_closed` (все шесть типов §4.1 принимаются, седьмой
  — `ValueError`).
- [ ] Реализация, suite, Commit:
  `feat(events): FilePipelineStateStore + пути пайплайна + best-effort sink`.

Трассируемость: §4.1, §4.2, P3, P8.

---

### Задача 7: verifier — doc-гейты ссылок (paths/links/anchors)

**Файлы:**
- Create: `src/disputatio/verifier/doc_refs.py` (парсер распознаваемых
  форм), `src/disputatio/verifier/doc_gates.py` (гейты 1–3)
- Test: `tests/verifier/test_doc_refs.py`, `tests/verifier/test_doc_gates_links.py`

**Интерфейсы (производит):**
- `parse_doc_refs(text: str) -> list[DocRef]`;
  `DocRef {kind: Literal["md_link","autolink","code_path","code_line_ref"],
  target: str, line: int, anchor: str | None, expected_text: str | None}` —
  **только однозначно распознаваемые формы**: Markdown inline/reference
  ссылки, автоссылки, пути и `file.py:42` в inline-code. Всё прочее не
  порождает DocRef (эвристики запрещены).
- `github_slug(heading: str, seen: dict[str, int]) -> str` — нормализация
  якорей: casefold, пробелы→дефисы, снятие пунктуации, Unicode как есть,
  percent-decoding входа, суффиксы `-1`, `-2` для повторов.
- `def gate_doc_paths(doc: Path, repo_root: Path) -> GateResult`,
  `gate_doc_links(...)`, `gate_doc_anchors(...)` — status pass/fail,
  `tail` — JSON-строки `{code, target, line}` (машинно-читаемый результат),
  `reason` — человекочитаемая сводка; неоднозначные формы → отдельный
  код `warning` в tail при `status: pass`.
- Containment: `resolve_inside(repo_root, target) -> Path | None` —
  нормализация `..` и symlink (`Path.resolve()`), выход из repo_root →
  нарушение с кодом `escape`.

**Шаги:**
- [ ] Red-тесты парсера: битые/валидные ссылки, reference-style, автолинк,
  inline-code путь, текст «похожий на путь» вне кода — НЕ распознан.
- [ ] Red-тесты slugger'а: регистр, пробелы, Unicode-заголовок,
  повторяющиеся заголовки (`-1`), percent-encoded входная ссылка.
- [ ] Red-тесты гейтов на fixture-репо (tmp_path): существующий/битый путь;
  разрешимая/битая относительная ссылка; существующий/битый якорь;
  symlink наружу → fail с `escape`; неоднозначная форма → pass + warning.
- [ ] Реализация, suite, Commit: `feat(verifier): doc-гейты paths/links/anchors`.

Трассируемость: §6 (гейты 1–3, правила распознавания/нормализации/containment).

---

### Задача 8: verifier — doc-line-refs, doc-scope, baseline

**Файлы:**
- Modify: `src/disputatio/verifier/doc_gates.py`
- Create: `src/disputatio/verifier/doc_verifier.py`
- Test: `tests/verifier/test_doc_gates_line_refs.py`,
  `tests/verifier/test_doc_verifier.py`

**Интерфейсы (производит):**
- `gate_doc_line_refs(doc, repo_root) -> GateResult` — `file:line`
  существует; если DocRef несёт `expected_text` (форма
  `` `file.py:42` («текст строки») `` — фиксируем ровно её) — строка 42
  совпадает с текстом; дрейф → fail с кодом `line_drift`.
- `gate_doc_scope(patch: str, allowed: tuple[str, ...]) -> GateResult` —
  разбор путей из `changes.patch`; путь вне allowed → fail с кодом
  `scope_escape`.
- `class DocVerifier:` реализация порта `Verifier` —
  `__init__(self, *, doc_paths: tuple[Path, ...], allowed: tuple[str, ...],
  repo_root: Path, patch_reader: Callable[[int], str],
  extra: Sequence[GateSpec] = ())`; `verify(round_no)` гоняет **baseline
  всегда** (все пять doc-гейтов; отключение невозможно по построению —
  параметра нет) + extra-гейты через существующий `run_gate`;
  агрегация — существующий `compute_overall`.

**Шаги:**
- [ ] Red-тесты line-refs: точная строка / дрейф / отсутствующий файл /
  line за EOF.
- [ ] Red-тесты scope: патч только по spec_path — pass; лишний файл — fail.
- [ ] Red-тест DocVerifier: baseline присутствует в отчёте всегда, extra
  добавляются, overall агрегируется; попытка конфига отключить baseline —
  негде (тест уровня конфига в задаче 12: неизвестный ключ гейта → отказ).
- [ ] Реализация, suite, Commit:
  `feat(verifier): doc-line-refs, doc-scope, DocVerifier с неотключаемым baseline`.

Трассируемость: §6 (гейты 4–5, baseline), P9-смежная граница doc-scope.

---

### Задача 9: runtime — RoundBoundaryPolicy и SessionLifecyclePolicy в drive()

**Файлы:**
- Modify: `src/disputatio/runtime/loop.py`, `runtime/steps.py`
  (точка вызова lifecycle вокруг авторского шага), `runtime/composition.py`
- Test: `tests/runtime/test_round_boundary.py`,
  `tests/runtime/test_lifecycle_policy.py`

**Интерфейсы (производит):**
- `drive(ctx, *, round_boundary: RoundBoundaryPolicy | None = None,
  lifecycle: SessionLifecyclePolicy | None = None) -> SessionState`.
- Точка опроса boundary: после того как `apply_decision` записал
  `CONTINUE` и write-ahead новую фазу, до исполнения её шага; `PARK` →
  `drive` возвращает текущее нетерминальное состояние.
- Lifecycle: `before_author_turn(state)` непосредственно перед запуском
  адаптера автора в шаге `PROPOSING`, `after_author_turn(state)` после
  возврата адаптера, до чтения/парсинга его вывода; исключение политики →
  `FAILED` сессии (существующий механизм невосстановимой ошибки).
- `resume_session(..., round_boundary=None, lifecycle=None)` — прокладка.

**Шаги:**
- [ ] Red-тест: `test_drive_without_policies_byte_identical` — прогон
  fake-сессии до терминала без политик, сравнение последовательности
  фаз/артефактов с эталоном (регресс-гарантия default'а).
- [ ] Red-тест: `test_park_returns_before_next_step` — policy паркует на
  раунде 1 с architectural-ревью: drive вернул нетерминальное состояние,
  фейковый автор раунда 2 НЕ вызывался, session.json write-ahead указывает
  `PROPOSING`.
- [ ] Red-тест: `test_lifecycle_called_each_proposing` — счётчик вызовов
  = числу раундов; `test_lifecycle_error_fails_session`.
- [ ] Реализация, suite, Commit:
  `feat(runtime): границы раунда и lifecycle-политики в drive()`.

Трассируемость: §7.1, P4.

---

### Задача 10: adapters — capability `path_write_deny`

**Файлы:**
- Modify: `src/disputatio/adapters/permissions.py`,
  `adapters/claude_code.py`, `adapters/codex.py`
- Test: `tests/adapters/test_path_write_deny.py`

**Интерфейсы (производит):**
- `AdapterCapabilities` (`adapters/permissions.py`) получает поля
  `path_write_deny: bool = False` и `deny_write_paths: tuple[str, ...] = ()`;
  `build_role_argv(Role.AUTHOR, caps)` перестаёт быть безусловным `[]`:
  при `path_write_deny and deny_write_paths` возвращает deny-аргументы
  `claude_code`, иначе — прежний `[]`.
- Слой **необязательный** (P9 в редакции после pair-ревью: якорь доверия —
  файловая граница `integrity_anchor`, не адаптер). Адаптер без capability
  допускается и старт не роняет; `ConfigError` тут не возникает вовсе.

**Шаги:**
- [ ] Red-тесты: `claude_code` c `path_write_deny=True` и
  `deny_write_paths=(".disputatio/**",)` собирает argv с deny-правилом
  (проверка построенной команды, без запуска CLI); адаптер без capability
  с теми же `deny_write_paths` — argv автора пуст, исключения нет;
  `test_author_argv_unchanged_by_default` (регресс: дефолт = прежнее `[]`).
- [ ] Реализация, suite, Commit:
  `feat(adapters): необязательная capability path_write_deny`.

Трассируемость: §9 (adapters), P9 (слой глубины, не якорь).

---

### Задача 11: context — промпты doc-автора и doc-ревьюера

**Файлы:**
- Create: `src/disputatio/context/doc_author.py`,
  `src/disputatio/context/doc_reviewer.py`
- Test: `tests/context/test_doc_prompts.py`

**Интерфейсы (производит):**
- `build_doc_author_prompt(*, contour, task_text, doc_paths,
  directive: str | None, adopted_findings: Sequence[Issue] = ())` — задача,
  пути документов, директива прошлого решения; `adopted_findings`
  (архитектурные находки для spec-rN+1) — внутри существующих тегов
  «данные, не инструкции» (`context/tags.py`).
- `build_doc_reviewer_prompt(*, contour, doc_texts, verification,
  checklist_ids)` — текст документов как данные; чеклист контура — как
  перечень обязательных полей `checklist` с требованием evidence по каждому.

**Шаги:**
- [ ] Red-тесты: находки обёрнуты data-тегами (grep по маркерам тегов);
  в промпте ревьюера присутствуют все id контурного чеклиста и ни одного
  чужого; результаты гейтов включены и при fail.
- [ ] Реализация (на существующих секциях `context/sections.py`), suite,
  Commit: `feat(context): промпты doc-контуров`.

Трассируемость: §5.1 (задачи автора), §5.2 (промпт-часть V-правил), §7.3
(находки как данные).

---

### Задача 12: runtime — расширение GitOps под adoption и reconciliation

**Файлы:**
- Modify: `src/disputatio/runtime/git.py` (протокол `GitOps` + реализация
  **`GitCli`** — единственная реализация в репо; `SubprocessGitOps` не
  существует)
- Modify (обязательно, иначе suite красный): все фейки `GitOps` в
  `tests/runtime/**` — 20 файлов; структурная проверка
  `assert isinstance(FakeGit(), GitOps)` в
  `tests/runtime/test_git_preflight.py:213` падает сразу после расширения
  `@runtime_checkable` протокола. Механическая правка: общий фейк-базис
  `tests/runtime/_fakes.py` со **всеми пятью** новыми методами (счёт важен:
  базис с четырьмя оставит structural-check красным), локальные фейки
  наследуют его
- Test: `tests/runtime/test_git_adoption.py`

**Интерфейсы (потребляет):** существующий `GitOps` (`diff_head`,
`commit_round`, `reset_hard`, `clean`), приватные `_checked`, `_run`,
`_env`, `_find_round_commit` — все в `runtime/git.py`.

**Интерфейсы (производит):** протокол `GitOps` расширяется **пятью**
методами (у операторских решений §3.1 SPEC-002 нет другого пути к git;
делать `subprocess` прямо в `pipeline_resume.py` нельзя — это второй слой
доступа к git мимо порта, INV-11):

```python
def head_sha(self) -> str: ...
def current_branch(self) -> str | None:
    """`git rev-parse --abbrev-ref HEAD`; `None` в detached HEAD.
    Нужен предусловию protected-ветки §3.1 — операции определения ветки в
    `runtime/git.py` сегодня нет вообще (проверено grep'ом), а без порта
    задача 13 полезла бы в subprocess мимо него."""
def status_entries(self) -> tuple[StatusEntry, ...]:
    """`git status --porcelain -uall` целиком, БЕЗ исключения путей:
    `StatusEntry {path: str, tracked: bool}`. Исключение `.disputatio/`
    делает потребитель (задача 16), а не порт: узкое правило §3.1 требует
    отличить собственные untracked control-файлы (легальны) от
    tracked-изменённых под `.disputatio/` (отказ), и порт, вырезающий их
    сам, эту информацию уничтожил бы."""
def commit_paths(self, paths: Sequence[str], subject: str,
                 *, trailer: str) -> str:
    """Коммитит РОВНО перечисленные пути (`git add -- <paths>`) с телом
    `subject\n\nDisputatio-Operation: <trailer>`; возвращает sha."""
def find_commit_by_trailer(self, trailer: str) -> str | None:
    """`git log --format=%H%x00%B` + поиск строки трейлера — идемпотентный
    повтор adoption не создаёт второй коммит. Поиск по subject не годится:
    subject одинаков у всех adoption'ов пайплайна."""
```

**Шаги:**
- [ ] Red-тесты на реальном tmp-git-репо (паттерн существующих тестов
  `runtime/git`): `head_sha` совпадает с `git rev-parse HEAD`;
  `current_branch` совпадает с `git rev-parse --abbrev-ref HEAD` и даёт
  `None` в detached HEAD; `status_entries` видит modified и untracked,
  **включая пути под `.disputatio/`**, и корректно проставляет `tracked`
  (untracked control-файл → `tracked=False`; закоммиченный и изменённый
  файл под `.disputatio/` → `tracked=True`);
  `commit_paths` коммитит только указанное (посторонний грязный файл
  остаётся вне коммита и в дереве); trailer попадает в тело;
  `find_commit_by_trailer` находит свой коммит и возвращает `None` для
  чужого; два adoption'а с разными trailer'ами различимы.
- [ ] Реализация; проверить, что фейки `GitOps` в существующих тестах
  обновлены (протокол расширился) — suite зелёный.
- [ ] Commit:
  `feat(runtime): GitOps — head_sha, current_branch, status_entries,
  commit_paths, trailer lookup`.

Трассируемость: §3.1 (adoption), §7.3 (cleanup), §8.1 (сверка worktree).

---

### Задача 13: runtime — конфиг пайплайна и предусловия run

**Файлы:**
- Create: `src/disputatio/runtime/pipeline_config.py`
- Test: `tests/runtime/test_pipeline_config.py`

**Интерфейсы (производит):**
- `PipelineConfig` (frozen dataclass): spec_path, plan_path,
  max_architectural_returns=2, soft_max_pipeline_tokens=0,
  soft_max_pipeline_wall_seconds=0, protected_branches=("master","main"),
  checklists (override или вендоренный дефолт задачи 2), extra_gates,
  `anchor_path: Path` — **каталог** журналов целостности P9
  (`anchor_root` в терминах §3.2 спеки), НЕ файл: сам журнал —
  `<anchor_path>/<anchor_id>.jsonl`, где `anchor_id` = `pipeline_id`
  из манифеста. Дефолт каталога — state-каталог пользователя без новой
  зависимости: `os.environ.get("XDG_STATE_HOME")` либо `~/.local/state`,
  плюс `disputatio/anchors`.
- `load_pipeline_config(path) -> PipelineConfig` — неизвестный ключ в
  `[pipeline.gates]`, совпадающий с baseline-именем, → `ConfigError`
  («baseline не отключается»).
- `check_run_preconditions(git: GitOps, workspace_root, config, slug)
  -> None` — чистое дерево через `GitOps.status_entries()` задачи 12 с тем
  же узким фильтром, что в задаче 16: блокирует **любая** запись, кроме
  untracked-путей под `.disputatio/` (собственный control plane; §4.1 —
  пайплайны сосуществуют под разными `<slug>`). То есть tracked-изменение
  блокирует всегда, посторонний untracked — тоже, а `.disputatio/**` с
  `tracked=False` — нет. Пропускать посторонний untracked **нельзя**:
  первый `PROPOSING` вызывает `reset_hard` + `clean()`
  (`runtime/steps.py:186-187`), а `clean()` работает по всему дереву минус
  каталог сессии (`runtime/git.py:421-425`) — файл был бы молча уничтожен
  без санкции оператора. Прецедент `preflight` не наследуется: его
  терпимость к untracked рассчитана на единственный ожидаемый untracked —
  `.disputatio/`. Текущая ветка (`GitOps.current_branch()`) ∉
  protected_branches — в detached HEAD (`None`) старт отклоняется,
  каталог пайплайна не существует, **канонизованный
  `anchor_path` резолвится вне `workspace_root`** (P9); нарушение →
  `ConfigError` с подготовительной командой в тексте
  (`git switch -c docs/<slug>`).

**Шаги:**
- [ ] Red-тесты: парсинг примера §3.2; baseline-переопределение → отказ;
  предусловия на tmp-git-репо: грязное дерево / protected ветка /
  существующий каталог → отказ с точным текстом; **tracked-изменение →
  отказ; посторонний untracked (`notes.txt` в корне) → отказ** — иначе его
  уничтожил бы `clean()` первого `PROPOSING`;
  `test_run_allowed_with_other_pipeline` — untracked-каталог другого
  `<slug>` под `.disputatio/pipelines/` старт НЕ блокирует (§4.1);
  ветка создаётся НЕ нами (проверка, что функция не мутирует репо).
- [ ] Red-тесты `anchor_path` (fail-closed, статические варианты — N7):
  прямой путь внутрь `workspace_root` → отказ; относительный путь
  резолвится от cwd и, попав внутрь дерева, → отказ; путь с `..`,
  выводящий обратно внутрь дерева, → отказ; symlink, ведущий внутрь
  дерева, → отказ (канонизация `expanduser`+`resolve`); дефолт
  вычисляется вне репозитория (`XDG_STATE_HOME` подменяется в тесте);
  та же проверка повторяется на `resume`, не только на `run`.
- [ ] Реализация, suite, Commit:
  `feat(runtime): конфиг пайплайна и fail-closed предусловия run`.

Трассируемость: §3.1 (предусловия), §3.2, §6 (baseline).

---

### Задача 14: runtime — экспорт готовой пары

*(Идёт до runner'а намеренно: runner исполняет intent `export`, и без
готового экспортёра его задача либо импортировала бы несуществующий
модуль, либо заводила незапланированную заглушку.)*

**Файлы:**
- Create: `src/disputatio/runtime/pipeline_export.py`
- Test: `tests/runtime/test_pipeline_export.py`

**Интерфейсы (потребляет):** `PipelineState` (задача 3), `GitOps.head_sha`
(задача 12).

**Интерфейсы (производит):**
- `def export_pipeline(state: PipelineState, *, workspace_root: Path,
  remote_url: str | None, branch: str | None, partial: bool = False)
  -> Path` — сигнатура и есть тот «порт», который runner получает
  инъекцией (`exporter: ExportFn` в задаче 15);
  `ExportFn = Callable[..., Path]` объявляется здесь же.
- `result/`: `pr_title.txt`, `pr_body.md` (сводка чеклистов/evidence,
  история контуров), `publish.txt` (`git push` + `gh pr create --draft`;
  quoting — `shlex.quote`; `remote_url is None or branch is None` →
  шаблон с `<REMOTE>`/`<BRANCH>` и строкой-предупреждением),
  `manifest.json` — **последним** (commit marker) с полным набором файлов
  и их sha256; старт экспорта удаляет из `result/` файлы вне нового
  набора; канонические байты: сортированные ключи, метки фактов
  (`created_at`, `at`) сохраняются, времени экспорта нет.

**Шаги:**
- [ ] Red-тесты: идемпотентность (два вызова — байт-в-байт, включая
  `manifest.json`); commit marker (обрыв до манифеста — набор невалиден по
  манифесту, повтор чинит и убирает stale-файл); честный partial
  (`converged: false`, причина эскалации, открытые находки);
  `publish.txt` без remote — шаблон с предупреждением; ветка со
  спецсимволом заквочена.
- [ ] Реализация, suite, Commit:
  `feat(runtime): идемпотентный экспорт пары с commit marker`.

Трассируемость: §8.2, P7.

---

### Задача 15: runtime — runner: фазы, интенты, контуры

**Файлы:**
- Create: `src/disputatio/runtime/pipeline_runner.py`
- Test: `tests/runtime/test_pipeline_runner.py`

**Интерфейсы (потребляет):** `PipelineStateStore` (задачи 4, 6),
`PipelineEventType`/sink (задача 6), расширенный `GitOps` (задача 12 —
`record_return` делает reset, без порта его взять неоткуда),
`PipelineConfig` (задача 13), `ExportFn` (задача 14),
`RoundBoundaryPolicy` (задачи 4, 9).

**Интерфейсы (производит):**
```python
class PipelineRunner:
    def __init__(self, *, store: PipelineStateStore, sink,
                 git: GitOps, session_driver: SessionDriver,
                 session_factory: SessionFactory, exporter: ExportFn,
                 now: Callable[[], datetime],
                 config: PipelineConfig) -> None: ...
    def run(self, slug: str, task_text: str) -> PipelineState: ...
    def advance(self, slug: str) -> PipelineState: ...
```
`SessionDriver = Callable[[Path, str, RoundBoundaryPolicy | None],
SessionState]` — инъекция: в тестах фейк со скриптованными артефактами,
реальный `drive`/`resume_session` подключается в задаче 17.

Механика: цикл §4.3 (intent → действие → результат либо chained-преемник);
`create_session` (снапшоты task/config/checklists с sha256, `entry_hashes`
с маркером `absent`, `artifact_root = sessions/<revision>`);
`run_session`; `finish_session` — интерпретация по **durable-состоянию**
(`session.json` + `decision.json` последнего раунда, не по возврату
драйвера); `record_return` (§7.3: `operation_id` из
`{session_id, round, sha256(review.json)}`; commit — одна запись:
transition + outcome + superseded_by + chained `create_session`);
`export` — вызов `exporter`. Внутри: `_recompute_budget(state)` —
`budget_used` пересчитывается из `session.json` **всех** сессий, включая
припаркованные, при каждой записи манифеста; soft-лимиты проверяются между
сессиями; `max_architectural_returns` → `ESCALATED`; pair-политика границы
раунда = «есть blocker/major с `defect_class: architectural` → `PARK`».

**Шаги:**
- [ ] Happy-path: spec converged → pair converged → EXPORTING → DONE;
  манифест: фазы, transitions, хеши снапшотов task/config/checklists
  (integrity-снапшотов в манифесте нет), вызов `exporter` ровно один.
- [ ] Возврат: pair паркуется (смешанное ревью arch + exec) → приоритет P6,
  spec-r2 создан, перекрытые ревизии получают `superseded_by`, outcome
  pair-r1 = `architectural_defect`, spec-r1 остался `converged`; pair-r2
  стартует без carried issues (проверка args создания).
- [ ] Бюджет: `test_budget_recomputed_no_double_count` — повторный
  `run_session` (replay после краха) не удваивает `tokens`/`wall_seconds`;
  припаркованная сессия входит в агрегат.
- [ ] Эскалации: DONE-через-DEADLOCK → `ESCALATED` (причина из
  `decision.json`); session `FAILED` → `FAILED` (P7: `exporter` не вызван);
  превышение `max_architectural_returns`; soft-лимит между сессиями.
- [ ] Crash-тесты — по одному на **каждую write-ahead границу внутри**
  многошаговых kind'ов, а не на kind целиком (§10 SPEC-002):
  (1) intent `create_session` записан, каталог не создан;
  (2) каталог создан, `session_started` не записан;
  (3) `run_session` записан, драйвер упал до записи результата;
  (4) `record_return`: intent записан, reset не выполнен;
  (5) reset выполнен, commit point не записан (идемпотентный повтор);
  (6) commit point записан, chained `create_session` не исполнен —
  преемник допроигран, предшественник не повторён;
  (7) `export` записан, экспорт прерван до `manifest.json`;
  (8) `FAILED → FAILED` идемпотентен (P8 — без дубликата transition);
  (9) `create_session`: снапшоты task/config/checklists записаны,
  `entry_hashes` ещё нет — повтор даёт те же байты снапшотов;
  (10) `run_session`: драйвер вернулся, результат не записан — повтор не
  прогоняет сессию заново (durable-состояние сессии уже терминально);
  (11) `finish_session`: интерпретация выполнена, запись outcome не
  случилась — replay даёт тот же outcome (идемпотентность **каждого**
  `kind`, §4.3 SPEC-002).
- [ ] Commit: `feat(runtime): PipelineRunner — фазы, интенты, контуры, возврат`.

Трассируемость: §2 (P1–P8), §4.2–4.3, §7.1–7.3, §10 (crash-минимум).

---

### Задача 16: runtime — pipeline-resume, анкер P9, операторские решения

**Файлы:**
- Create: `src/disputatio/runtime/pipeline_resume.py`,
  `src/disputatio/runtime/pipeline_integrity.py` (только политика —
  журнал живёт в `events`, задача 6),
  `src/disputatio/runtime/pipeline_adopt.py`
- Test: `tests/runtime/test_pipeline_resume.py`,
  `tests/runtime/test_pipeline_integrity.py`,
  `tests/runtime/test_pipeline_adopt.py`

**Интерфейсы (потребляет):** расширенный `GitOps` (задача 12),
`IntegritySnapshot`/`AppendOnlyEntry` (задача 3; `ImmutableEntry` в
модели нет — immutable-часть плоская `dict[str, str]`),
`IntegrityAnchor` (задача 6, пакет `events`),
`SessionLifecyclePolicy` (задачи 4, 9), `PipelineRunner` (задача 15).

**Интерфейсы (производит):**
- `resume(slug, *, decision: Literal["discard_round","adopt_external"]
  | None = None)` — строгий порядок §8.1: (0) сверка по анкеру, (1) чтение
  манифеста (сессии с `outcome`/`superseded_by` ≠ null не возобновляются),
  (2) **read-only** обнаружение архитектурного дефекта, (3) сверка
  worktree, (4) мутирующая фаза, (5) session-resume с политиками.
- `classify_worktree(git: GitOps, state) -> Literal["clean","legal_patch",
  "unattributed"]`; `unattributed` без `decision` → `ExternalEditError`
  с дифом в тексте.
- `class PipelineIntegrityPolicy` —
  реализация `SessionLifecyclePolicy`: `before_author_turn` пишет снапшот
  **только в анкер** (N1: две файловые границы одной атомарной операцией
  не согласовать, и штатный крах между записями читался бы как подмена;
  манифест снапшота не хранит вовсе), `after_author_turn` сверяет control
  plane против анкера; расхождение → исключение →
  `FAILED (invariant_violation)`. Запись идемпотентна по
  `{session_id, round, operation_id}`.
- `adopt_external(...)` / `discard_round(...)` — интенты §3.1: scope по
  `GitOps.status_entries()` — порт отдаёт статус целиком, фильтрует
  потребитель: запись с `tracked=False` под `.disputatio/` игнорируется
  (это собственные untracked-файлы пайплайна), запись с `tracked=True`
  под `.disputatio/` — **отказ** (внешняя правка control plane, §3.1);
  в остатке допустимы только `spec_path`/`plan_path`, новый untracked
  документ легален, иначе отказ целиком; patch →
  `adoptions/<operation_id>.patch`; чекпоинт —
  `commit_paths([документы], "disputatio: operator adopt <slug>",
  trailer=operation_id)` с предварительным `find_commit_by_trailer`
  (идемпотентность); commit point — одна запись манифеста: decision +
  `abandoned` + `superseded_by` + transition (`external_spec_adopt` при
  затронутом `spec_path`; `architectural_defect`, если причина только
  дефект; обе причины → `external_spec_adopt`, находки в evidence) +
  chained `create_session` с `base_commit` = sha чекпоинта.

**Шаги:**
- [ ] Red-тесты порядка: дефект + грязное неатрибутируемое дерево →
  остановка **до любых мутаций** (spy на `GitOps`: ни `reset_hard`, ни
  `commit_paths` не вызваны); припаркованная/`abandoned` сессия не
  возобновляется; легальный `changes.patch`-диф проходит.
- [ ] Red-тесты анкера: **подмена манифеста ловится сверкой против
  анкера** — сценарий, ради которого анкер вынесен из дерева: манифест
  автору достижим, анкер нет, а снапшота в манифесте не существует, так
  что подделывать нечего; усечение журнала → `FAILED`;
  легальный append оркестратора проходит prefix-property; kill между
  before и after → resume ловит по анкеру; `anchor_path` внутри
  `workspace_root` → отказ старта (тест уровня задачи 13, здесь —
  регресс на конструировании политики); **крах между append'ом в анкер и
  началом хода автора**: повтор пишет ту же строку (идемпотентность по
  `{session_id, round, operation_id}`), сверка не считает это подменой.
- [ ] Red-тесты adoption (маршрут): только `plan_path` в pair → новая
  pair-ревизия; `spec_path` в pair → spec-ревизия и reason
  `external_spec_adopt` даже без architectural finding; обе причины →
  один outcome `abandoned`, `record_return` не вызван; чужой tracked
  путь → отказ; чужой untracked путь → отказ; новый untracked
  `plan_path` → принят и попал в чекпоинт; **untracked-файлы самого
  пайплайна под `.disputatio/` adoption не ломают** (иначе он не проходил
  бы никогда), а tracked-изменённый путь под `.disputatio/` → отказ.
- [ ] Red-тесты adoption (crash, по границам): (1) intent записан, patch не
  создан; (2) patch создан, чекпоинт не сделан; (3) чекпоинт сделан,
  commit point не записан — повтор находит коммит по trailer'у и второго
  не создаёт; (4) commit point записан, chained `create_session` не
  исполнен; (5) `discard_round`: intent записан, reset не выполнен;
  (6) reset выполнен, decision не записан — provenance не потеряна.
- [ ] Red-тест сохранности: принятая правка переживает первый `PROPOSING`
  новой ревизии (`base_commit` = чекпоинт) и повторный resume.
- [ ] Реализация, suite, Commit:
  `feat(runtime): pipeline-resume, анкер целостности, операторские решения`.

Трассируемость: §3.1, §8.1, P3, P9.

---

### Задача 17: CLI `disp pipeline` + сквозные интеграционные тесты

**Файлы:**
- Modify: `src/disputatio/cli.py`, `src/disputatio/runtime/composition.py`
- Create: `tests/integration/__init__.py` (каталога нет — создаётся этой
  задачей), `tests/integration/test_pipeline_e2e.py`

**Интерфейсы (потребляет):** всё предыдущее; здесь же реальный
`session_driver` = `drive`/`resume_session` с политиками (задача 9).

**Интерфейсы (производит):**
- Команды §3.1: `disp pipeline run|resume|status|export`; `resume`
  принимает `--discard-round`/`--adopt-external` (взаимоисключающи);
  коды выхода: DONE-конвергенция → 0, `ESCALATED` → ненулевой, `FAILED` →
  ненулевой без автоэкспорта.
- Composition root собирает runner из реальных реализаций; валидация
  `validate_doc_review` включается по `Mode.DOCUMENT` в существующем слое
  schema-retry.

**Шаги (по одному сценарию за шаг, red → green):**
- [ ] `test_status_is_read_only` — `status` не пишет на диск (снимок mtime
  и содержимого каталога до/после).
- [ ] Happy-path двух контуров на fake-адаптерах до `result/` с проверкой
  `manifest.json`.
- [ ] Архитектурный возврат со смешанным ревью → spec-r2 → полная pair-r2
  без наследства (P5/P6).
- [ ] Анти-сикофантия раунда 1 для `Mode.DOCUMENT`: скриптованный approve
  раунда 1 не даёт сходимости.
- [ ] V6-гарантия: approve с fail-пунктом чеклиста гибнет в schema-retry и
  до `decide()` не доходит (spy — обёртка вокруг `decide` в composition
  тестового прогона; `core/deciding.py` не редактируется).
- [ ] Verification fail (битая ссылка в спеке): ревью состоялось, но
  `CONVERGED` заблокирован до починки.
- [ ] Сохранность постороннего untracked: файл `notes.txt` в корне →
  `run` отказывает до создания чего-либо; после его удаления `run`
  проходит и `clean()` первого `PROPOSING` уничтожать нечего.
- [ ] Commit: `feat(runtime): CLI disp pipeline + сквозные сценарии`.

Трассируемость: §3.1 (CLI), §5.1 (анти-сикофантия), §10, V6.

---

## Матрица трассируемости SPEC-002 → задачи

Читать как обязательство: строка означает, что перечисленные задачи
покрывают раздел **целиком**; частичное покрытие названо явно.

| Раздел SPEC-002 | Задачи | Примечание |
|---|---|---|
| §2 P1–P8 | 3 (таблица, модели), 6 (guard prefix-equality + читатель с дедупликацией по `operation_id`), 15 (поведение) | — |
| §2 P9 | 3 (форма снапшота), 4 + 9 (lifecycle-seam), 6 (`IntegrityAnchor` в `events`), 10 (необязательный слой), 13 (fail-closed `anchor_root`), 16 (политика и сверка) | снапшот только в анкере; манифест несёт `anchor_id` |
| §3.1 CLI и предусловия | 12 (`current_branch`, `status_entries`), 13 (узкий фильтр `.disputatio/**`), 17 (команды) | посторонний untracked блокирует старт: его уничтожил бы `clean()` |
| §3.1 решения оператора | 12 (`status_entries` с классификацией tracked), 16 (фильтр `.disputatio/` и маршруты) | порт статус не режет — режет потребитель |
| §3.2 конфиг | 13 (парсинг, `anchor_path` — каталог), 15 (снапшоты в `create_session`) | журнал — `<anchor_path>/<anchor_id>.jsonl` |
| §4.1 layout, artifact_root | 5, 6 | включая закрытый словарь событий |
| §4.2 манифест | 3 (схема, `anchor_id`), 6 (хранилище, prefix-equality, анкер), 15 (`budget_used`, transitions), 16 (`operator_decisions`) | снапшоты — не в манифесте, а в анкере (задача 6) |
| §4.3 интенты и chaining | 3 (enum), 15 (11 границ краха core-kind'ов, включая `finish_session`), 16 (6 операторских границ) | идемпотентность каждого `kind` доказана поимённо |
| §5.1 Mode.DOCUMENT, v2 | 1, 17 (анти-сикофантия) | — |
| §5.2 схема ревью, V1–V8 | 1 (закрытые формы evidence), 2 (машинная часть V1–V8), 11 (недетерминируемая часть V8 — требование промпта ревьюеру), 17 (V6 сквозняком) | V8 машинно enforced только там, где связь выводима (S1) |
| §5.3 чеклисты | 2 (вендоренный дефолт), 13 (override) | — |
| §6 doc-гейты, baseline | 7, 8, 13 (отказ отключения) | — |
| §7.1 порты границ | 4 (объявление), 9 (вызовы в `drive`) | — |
| §7.2 терминалы | 15 | — |
| §7.3 возврат | 12 (git-примитивы + обновление 20 файлов фейков), 15 (reconciliation, `GitOps` в конструкторе runner'а), 16 (гейт reset на resume) | — |
| §8.1 resume, внешняя правка | 12 (git), 16 (порядок и классификация) | — |
| §8.2 экспорт | 14 | — |
| §9 раскладка по пакетам | структура файлов всех задач; `IntegrityAnchor` — в `events` (задача 6), `runtime` держит только политику (задача 16) | contracts→events→verifier→context→adapters→runtime |
| §10 тесты | 15 (11 core write-ahead границ), 16 (6 операторских границ + анкер), 17 (6 сквозных) + red-тесты задач 1–14 | перечислены поимённо, а не счётом |

**Осознанно вне плана** (§11 SPEC-002): автопубликация PR, гейт
трассируемости REQ-id, жёсткие пайплайн-лимиты, N-stage, discovery,
erratum SPEC-001, изоляция уровня ОС для автора.

**Имена, введённые планом, а не спекой** (implementation details, не
нормативные интерфейсы; переименование при реализации допустимо, если
сохранены семантика и JSON-форма из SPEC-002): `ArtifactEvidence`,
`GateEvidence`, `ChecklistItem`, `AppendOnlyEntry`,
`DocRef`, `BoundaryVerdict`, `PipelineConfig`, `PipelineEventType`,
`DocVerifier`, `IntegrityAnchor` (имя плановое, пакет нормативный —
`events`, §9), `ExportFn`, `SessionDriver`,
`ALLOWED_TRANSITIONS`, `SessionRecord`, `Transition`, `OperatorDecision`,
`NextAction`, `EvidenceLink`, `IntegritySnapshot`,
`StatusEntry`,
`FilePipelineStateStore`, `PipelineEventSink`, `read_pipeline_events`,
`PipelineIntegrityPolicy`, `PipelineRunner`, `validate_doc_review`,
`SPEC_CHECKLIST`/`PAIR_CHECKLIST`.
