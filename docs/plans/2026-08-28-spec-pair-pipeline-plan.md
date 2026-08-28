# План имплементации SPEC-002 — пайплайн полировки пары «спека + план»

> **Для agentic-исполнителей:** REQUIRED SUB-SKILL:
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans — задача за задачей, шаги — чекбоксы `- [ ]`.

**Цель:** автоматизировать два вложенных цикла полировки документов
(spec-контур → pair-контур → экспорт готовой к публикации пары) поверх
протокола раундов SPEC-001, без изменения ядра FSM сессии.

**Архитектура:** пайплайн поверх стандартных resumable-сессий; runner ведёт
контуры через два новых runtime-порта (`RoundBoundaryPolicy`,
`SessionLifecyclePolicy`); всё состояние — write-ahead манифест
`disputatio/pipeline/v1`; doc-артефакты — схема `disputatio/v2`.

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
- `class EvidenceRef(ArtifactChild): kind: Literal["artifact","gate"];
  ref: str; lines: str | None = None`
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
  evidence — ValidationError), `test_defect_class_optional_default_none`.
- [ ] Запустить, убедиться в падении: `uv run pytest tests/contracts/test_schema_v2.py -q`.
- [ ] Минимальная реализация; suite + pyrefly + ruff.
- [ ] Commit: `feat(contracts): схема disputatio/v2 — Mode.DOCUMENT, checklist, defect_class`.

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
  `IntegritySnapshot {session_id, round, hashes: dict[str, str]}`,
  `PipelineState(ArtifactBase)` — все поля §4.2.

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
  `feat(contracts): порты PipelineStateStore, RoundBoundaryPolicy, SessionLifecyclePolicy`.

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
  `src/disputatio/events/pipeline_events.py`
- Test: `tests/events/test_pipeline_store.py`

**Интерфейсы (производит):**
- `pipeline_dir(workspace_root, slug)` → `.disputatio/pipelines/<slug>` и
  производные (`sessions_dir`, `adoptions_dir`, `result_dir`,
  `events_path`, `manifest_path`); грамматика slug
  `[a-z0-9][a-z0-9._-]{0,63}` — валидация здесь.
- `FilePipelineStateStore(workspace_root)` — реализация порта; save —
  `atomic_write`; **guard неизменяемости**: save отклоняет (`ValueError`)
  изменение уже ненулевого `outcome` и укорачивание append-only коллекций
  (`transitions`, `spec_sessions`, `pair_sessions`, `operator_decisions`).
- `PipelineEventSink` — best-effort/zero-or-more: `emit(event)` с
  `operation_id` в payload; на открытии — ремонт хвоста (последняя строка
  без `\n` или невалидный JSON → усечение).

**Шаги:**
- [ ] Red-тесты: атомарность (temp+rename — нет частичного файла);
  `test_outcome_rewrite_rejected`; `test_transitions_shrink_rejected`;
  `test_tail_repair_truncates_partial_line`;
  `test_slug_grammar_rejected`.
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
- У адаптеров классовый атрибут/метод
  `supports_path_write_deny: bool` и параметр конструктора
  `deny_write_paths: tuple[str, ...] = ()`; `claude_code` мапит в свои
  permission-deny правила (той же механикой, что read-only ревьюер —
  см. `permissions.py`), адаптер без поддержки при непустом
  `deny_write_paths` кидает `ConfigError` на конструировании (fail-closed).

**Шаги:**
- [ ] Red-тесты: claude_code собирает CLI-аргументы с deny на
  `.disputatio/**` (проверка построенной команды, без запуска CLI);
  неподдерживающий адаптер + deny → `ConfigError`;
  пустой deny → поведение неизменно.
- [ ] Реализация, suite, Commit:
  `feat(adapters): capability path_write_deny (fail-closed)`.

Трассируемость: §9 (adapters), P9 (якорь доверия).

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

### Задача 12: runtime — конфиг пайплайна и предусловия run

**Файлы:**
- Create: `src/disputatio/runtime/pipeline_config.py`
- Test: `tests/runtime/test_pipeline_config.py`

**Интерфейсы (производит):**
- `PipelineConfig` (frozen dataclass): spec_path, plan_path,
  max_architectural_returns=2, soft_max_pipeline_tokens=0,
  soft_max_pipeline_wall_seconds=0, protected_branches=("master","main"),
  checklists (override или вендоренный дефолт задачи 2), extra_gates.
- `load_pipeline_config(path) -> PipelineConfig` — неизвестный ключ в
  `[pipeline.gates]`, совпадающий с baseline-именем, → `ConfigError`
  («baseline не отключается»).
- `check_run_preconditions(workspace_root, config, slug) -> None` —
  чистое дерево, текущая ветка ∉ protected_branches, каталог пайплайна не
  существует; нарушение → `ConfigError` с подготовительной командой в
  тексте (`git switch -c docs/<slug>`).

**Шаги:**
- [ ] Red-тесты: парсинг примера §3.2; baseline-переопределение → отказ;
  предусловия на tmp-git-репо: грязное дерево / protected ветка /
  существующий каталог → отказ с точным текстом; ветка создаётся НЕ нами
  (проверка, что функция не мутирует репо).
- [ ] Реализация, suite, Commit:
  `feat(runtime): конфиг пайплайна и fail-closed предусловия run`.

Трассируемость: §3.1 (предусловия), §3.2, §6 (baseline).

---

### Задача 13: runtime — runner: фазы, интенты, контуры

**Файлы:**
- Create: `src/disputatio/runtime/pipeline_runner.py`
- Test: `tests/runtime/test_pipeline_runner.py` (fake-сессии: runner
  получает `session_driver: Callable` — инъекция, реальный drive
  подключается в задаче 16)

**Интерфейсы (производит):**
- `class PipelineRunner: __init__(self, *, store: PipelineStateStore,
  sink, session_driver, session_factory, now, config: PipelineConfig)`.
- `run(slug, task_text)` / `advance()` — цикл §4.3: intent → действие →
  результат/преемник; полный machinery: `create_session` (снапшоты
  task/config/checklists c sha256, entry_hashes c маркером `absent`,
  artifact_root = `sessions/<revision>`), `run_session`, `finish_session`
  (интерпретация терминалов по durable-состоянию §7.2 — читает
  session.json/decision.json, не возврат driver'а), `record_return`
  (reconciliation §7.3: детерминированный operation_id из
  `{session_id, round, sha256(review.json)}`; commit — одна запись:
  transition+outcome+superseded_by+chained create_session), `export`
  (задача 15).
- Внутри: `_interpret(session_record) -> SessionOutcome`;
  `budget_used` пересчитывается из session.json всех сессий при каждом
  save; soft-лимиты проверяются между сессиями → ESCALATED;
  `max_architectural_returns` → ESCALATED; RoundBoundaryPolicy для pair =
  «есть blocker/major c defect_class=architectural → PARK».

**Шаги (по одному red→green→commit на группу):**
- [ ] Happy-path: spec converged → pair converged → EXPORTING → DONE;
  манифест: фазы, transitions, хеши снапшотов.
- [ ] Возврат: pair паркуется (fake-ревью со смешанными находками arch +
  exec) → приоритет P6, spec-r2 создан, старые ревизии `superseded_by`,
  outcome pair-r1 = architectural_defect, spec-r1 остался converged;
  pair-r2 стартует без carried issues (проверка args создания).
- [ ] Эскалации: session DONE-через-DEADLOCK → ESCALATED (причина из
  decision.json); session FAILED → FAILED (P7: экспорт не вызван);
  превышение max_architectural_returns и soft-лимитов между сессиями.
- [ ] Crash-тесты: kill (исключение fake-driver'а) на каждом kind
  интента; resume-объект (`advance()` заново по манифесту) допроигрывает
  без дубликатов: «каталог сессии создан, session_started не записан»;
  «reset выполнен, transition не записан» (идемпотентный повтор);
  chained-intent: падение на create_session после commit'а возврата —
  преемник допроигран, предшественник не повторён;
  `FAILED → FAILED` идемпотентен (P8 — нет дубликата transition).
- [ ] Commit: `feat(runtime): PipelineRunner — фазы, интенты, контуры, возврат`.

Трассируемость: §2 (P1–P8), §4.2–4.3, §7.1–7.3.

---

### Задача 14: runtime — pipeline-resume, P9, операторские решения

**Файлы:**
- Create: `src/disputatio/runtime/pipeline_resume.py`,
  `src/disputatio/runtime/pipeline_integrity.py`
- Test: `tests/runtime/test_pipeline_resume.py`,
  `tests/runtime/test_pipeline_integrity.py`

**Интерфейсы (производит):**
- `resume(slug, *, decision: Literal["discard_round","adopt_external"]
  | None = None)` — строгий порядок §8.1: (0) P9-сверка по
  `integrity_snapshot`, (1) манифест (outcome/superseded_by ≠ null — не
  возобновлять), (2) read-only обнаружение дефекта, (3) сверка worktree,
  (4) мутирующая фаза, (5) session-resume c политиками.
- Модель внешней правки: `classify_worktree(...) ->
  Literal["clean","legal_patch","unattributed"]` — HEAD+чистое / диф
  байт-в-байт = changes.patch / прочее; `unattributed` без `decision` →
  `ExternalEditError` с дифом в тексте.
- `PipelineIntegrityPolicy` — реализация `SessionLifecyclePolicy`:
  before = записать `integrity_snapshot` (атомарный save манифеста),
  after = сверка (равенство хешей / prefix-property журналов),
  расхождение → исключение → `FAILED (invariant_violation)`.
- Операторские решения — интенты §3.1: `discard_round` (intent до reset,
  commit = OperatorDecision); `adopt_external` — scope по полному
  `git status` (tracked+untracked; только spec_path/plan_path, новый
  untracked документ легален; иначе отказ целиком), patch →
  `adoptions/<operation_id>.patch`, git-чекпоинт c trailer'ом
  `Disputatio-Operation: <operation_id>` (идемпотентный поиск по
  trailer'у), commit point — одна запись: decision + abandoned +
  superseded_by + transition (`external_spec_adopt` при spec_path,
  `architectural_defect` при только-дефекте, обе причины →
  external_spec_adopt + дефект в evidence) + chained create_session с
  base_commit = чекпоинт.

**Шаги:**
- [ ] Red-тесты порядка: дефект + грязное неатрибутируемое дерево →
  остановка ДО любых мутаций (ни reset, ни replay — spy на git-слое);
  припаркованная/abandoned сессия не resume-ится; легальный
  changes.patch-диф проходит.
- [ ] Red-тесты P9: подмена манифеста «автором» между before/after →
  FAILED; усечение журнала → FAILED; легальный append проходит; kill
  между before и after → resume ловит по durable snapshot'у;
  адаптер без path_write_deny в document-режиме → отказ preflight.
- [ ] Red-тесты adoption: маршрутизация по путям (§3.1 — три случая +
  чужой tracked/untracked путь → отказ); правка переживает первый
  PROPOSING новой ревизии (base_commit) и resume; crash после patch,
  после git-чекпоинта, до commit point — по одному тесту, чекпоинт не
  дублируется; discard: падение между reset и записью решения не теряет
  provenance; outcome ровно один (`abandoned`), record_return не вызван.
- [ ] Реализация, suite, Commit:
  `feat(runtime): pipeline-resume, P9-целостность, операторские решения`.

Трассируемость: §3.1 (решения), §8.1, P9, P3.

---

### Задача 15: runtime — экспорт готовой пары

**Файлы:**
- Create: `src/disputatio/runtime/pipeline_export.py`
- Test: `tests/runtime/test_pipeline_export.py`

**Интерфейсы (производит):**
- `export_pipeline(state, *, workspace_root, partial: bool = False) -> Path`:
  `result/` = `pr_title.txt`, `pr_body.md` (сводка чеклистов/evidence,
  история контуров), `publish.txt` (remote/branch из `git remote get-url`
  + текущей ветки, shell-quoting через `shlex.quote`; remote неопределим →
  шаблон `<REMOTE>` с явной строкой-предупреждением), `manifest.json` —
  **последним** (commit marker), перечисляет полный набор файлов с sha256;
  старт экспорта удаляет из result/ файлы вне нового набора; канонические
  байты — сортированные ключи, `created_at`/`at` — фиксированные метки
  фактов, времени экспорта нет.
- CLI-коды: DONE-конвергенция → 0, ESCALATED → ненулевой; FAILED —
  экспорт только `--partial`.

**Шаги:**
- [ ] Red-тесты: идемпотентность (два вызова — байт-в-байт, включая
  manifest); commit marker (обрыв до manifest — набор невалиден по
  манифесту, повтор чинит и убирает stale-файл); честный partial
  (`converged: false`, причина эскалации, открытые находки);
  publish.txt при отсутствующем remote — шаблон с предупреждением,
  quoting ветки со спецсимволом.
- [ ] Реализация, suite, Commit:
  `feat(runtime): идемпотентный экспорт пары с commit marker`.

Трассируемость: §8.2, P7, m1/m7-правки (§4.2 про метки).

---

### Задача 16: CLI `disp pipeline` + сквозные интеграционные тесты

**Файлы:**
- Modify: `src/disputatio/cli.py`, `src/disputatio/runtime/composition.py`
- Test: `tests/integration/test_pipeline_e2e.py`

**Интерфейсы (производит):**
- Команды §3.1: `disp pipeline run|resume|status|export` (status — только
  чтение манифеста, тест: никаких записей на диск); composition root
  собирает runner из реальных реализаций (v2-валидация ревью doc-сессий
  включается по Mode.DOCUMENT в существующем слое schema-retry).
- Сквозные тесты на fake-адаптерах (скриптованные ответы автора/ревьюера):
  1. полный happy-path двух контуров до `result/` c проверкой manifest;
  2. архитектурный возврат со смешанным ревью → spec-r2 → полная pair-r2
     без наследства (P5/P6);
  3. анти-сикофантия раунда 1 для document: скриптованный approve
     раунда 1 не даёт сходимости — форс содержательного цикла;
  4. V6-гарантия: скриптованный approve с fail-пунктом чеклиста гибнет в
     schema-retry и до `decide()` не доходит (spy на decide —
     импорт-обёртка в тесте, `core/deciding.py` не тронут);
  5. verification fail (битая ссылка в спеке) → ревью состоялось, но
     converged заблокирован до починки.
- [ ] Red → green по одному сценарию за шаг; suite; Commit:
  `feat(runtime): CLI disp pipeline + сквозные сценарии`.
- [ ] Финал: `docs/workstream-setup.md` не трогаем (вне scope);
  `TODO.md` — пункт `spec-pair-polish-automation` пометить прогрессом
  ссылкой на PR пары (отдельным коммитом в PR пары не входит — правка
  TODO делается в самом PR реализации).

Трассируемость: §3.1 (CLI), §10 (интеграционный минимум), V6.

---

## Матрица трассируемости SPEC-002 → задачи

| Раздел SPEC-002 | Задачи |
|---|---|
| §2 FSM, P1–P9 | 3 (таблица/модели), 6 (P3/P8-guard), 13 (P1–P8), 14 (P9) |
| §3.1 CLI, предусловия, решения оператора | 12, 14, 16 |
| §3.2 конфиг | 12 |
| §4.1 layout, artifact_root | 5, 6 |
| §4.2 манифест | 3, 6, 13 |
| §4.3 интенты, chaining | 3, 13, 14 |
| §5.1 Mode.DOCUMENT, v2 | 1, 16 |
| §5.2 схема ревью, V1–V8 | 1, 2, 16 (V6) |
| §5.3 чеклисты | 2, 12 (override) |
| §6 doc-гейты, baseline | 7, 8, 12 (отказ отключения) |
| §7.1 порты границ | 4, 9 |
| §7.2 терминалы | 13 |
| §7.3 возврат | 13, 14 (cleanup-гейт) |
| §8.1 resume, внешняя правка | 14 |
| §8.2 экспорт | 15 |
| §9 раскладка | вся структура файлов плана |
| §10 тесты | распределены по задачам 1–16 |

Не покрывается планом (вне v1, §11 SPEC-002): автопубликация PR, гейт
трассируемости REQ-id, жёсткие пайплайн-лимиты, N-stage, discovery,
erratum SPEC-001.
