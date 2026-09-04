---
traces_to:
- behaviour-spec
upstream_hashes:
  behaviour-spec: cd5ac77984be516d3c2f6bd08f3858670daf0dd1
spec_stage: tasks
status: approved
version: 2
generated_by: fleet-agent
generated_at: '2026-09-03T03:53:05'
source_prompt_version: ''
validation: warn
approved_by: verifier-tests
approved_at: '2026-09-02T23:58:37Z'
---

## Milestone 1: Immutable session semantics: resume сверяет неизменяемую половину [pipeline] со снапшотом fail-closed (disputatio#65)

Сгенерировано task_bridge из behaviour-spec бандла WS-disputatio-65 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Draft: исполнение только после человеческого approve.

### TASK-001: Run атомарно фиксирует версионированную immutable-модель (+3 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-01, BEH-12, BEH-13, BEH-15.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-01 (—BEH-15)

**Checklist:**
- [x] реализовать BEH-01: Run атомарно фиксирует версионированную immutable-модель
- [x] реализовать BEH-12: Недоказуемая семантика запрещает продолжение без fallback
- [x] реализовать BEH-13: Каждый источник доказательства проверяется и согласуется
- [x] реализовать BEH-15: Ошибка доказательства различает безопасные причины
- [x] проверка группы: tests/runtime/test_pipeline_semantic_proof.py::test_run_commits_versioned_proof_atomically (kind: integration), tests/runtime/test_pipeline_semantic_proof.py::test_unprovable_semantics_fail_closed_without_fallback (kind: atp), tests/runtime/test_pipeline_semantic_proof.py::test_all_proof_sources_require_integrity_and_consistency (kind: atp), tests/runtime/test_pipeline_semantic_proof.py::test_proof_errors_are_distinct_safe_and_actionable (kind: contract) зелёные на BEH-01, BEH-12, BEH-13, BEH-15

**Traces to:** [FR-01, FR-11, FR-12, FR-15]

### TASK-002: Эквивалентное TOML-представление даёт ту же модель (+8 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-02, BEH-03, BEH-04, BEH-05, BEH-06, BEH-07, BEH-14, BEH-19, BEH-21.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-02 (—BEH-21)
**Depends on:** [TASK-001]

**Checklist:**
- [x] реализовать BEH-02: Эквивалентное TOML-представление даёт ту же модель
- [x] реализовать BEH-03: Пути канонизируются без привязки к машине
- [x] реализовать BEH-04: Полная семантика pair-чеклистов неизменяема
- [x] реализовать BEH-05: Полная семантика document-чеклиста неизменяема
- [x] реализовать BEH-06: Все свойства и порядок дополнительных gates неизменяемы
- [x] реализовать BEH-07: Четыре mutable control не создают drift
- [x] реализовать BEH-14: Drift-диагностика точна и не раскрывает чувствительные значения
- [x] реализовать BEH-19: Каждая строка immutable-классификации имеет регрессионный пример
- [x] реализовать BEH-21: Классификация и исполнимый контракт документированы единообразно
- [x] проверка группы: tests/runtime/test_pipeline_semantics.py::test_equivalent_toml_and_explicit_defaults_have_one_projection (kind: contract), tests/runtime/test_pipeline_semantics.py::test_document_paths_are_relative_posix_and_machine_independent (kind: contract), tests/runtime/test_pipeline_semantics.py::test_pair_checklist_semantics_are_immutable (kind: atp), tests/runtime/test_pipeline_semantics.py::test_document_checklist_semantics_are_immutable (kind: atp), tests/runtime/test_pipeline_semantics.py::test_gate_order_and_all_properties_are_immutable (kind: atp), tests/runtime/test_pipeline_semantics.py::test_mutable_controls_apply_without_semantic_drift (kind: atp), tests/runtime/test_pipeline_semantics.py::test_drift_diagnostic_is_sorted_specific_and_redacted (kind: contract), tests/runtime/test_pipeline_semantics.py::test_every_immutable_classification_row_is_enforced (kind: atp), tests/runtime/test_pipeline_semantics.py::test_schema_parser_and_canonicalizer_classifications_match (kind: manual) зелёные на BEH-02, BEH-03, BEH-04, BEH-05, BEH-06, BEH-07, BEH-14, BEH-19, BEH-21

**Traces to:** [FR-02, FR-17, FR-03, FR-04, FR-10, FR-05, FR-06, FR-14, FR-18, FR-07, FR-19]

### TASK-003: Неизвестный pipeline-ключ отвергается закрытой схемой
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-08.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-08
**Depends on:** [TASK-002]

**Checklist:**
- [x] реализовать BEH-08: Неизвестный pipeline-ключ отвергается закрытой схемой
- [x] проверка группы: tests/runtime/test_pipeline_config.py::test_unknown_pipeline_keys_fail_closed_at_every_schema_level (kind: atp) зелёные на BEH-08

**Traces to:** [FR-07]

### TASK-004: P9 предшествует чтению ожидаемой семантики (+4 смежных BEH)
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-09, BEH-10, BEH-11, BEH-17, BEH-20.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-09 (—BEH-20)
**Depends on:** [TASK-003]

**Checklist:**
- [x] реализовать BEH-09: P9 предшествует чтению ожидаемой семантики
- [x] реализовать BEH-10: Semantic comparison занимает единственную раннюю позицию resume
- [x] реализовать BEH-11: Drift останавливает resume без побочных эффектов
- [x] реализовать BEH-17: Повтор после semantic-отказа не видит новых артефактов
- [x] реализовать BEH-20: Отказ semantic comparison сохраняет действующие контракты
- [x] Закрыть crash-окно BEH-01: создать криптографически связанную genesis/P9-запись якоря на commit point run и проверять её до чтения semantic proof при resume (PR #90, review round 9); TASK-004 не DONE, пока genesis-запись и crash/tamper-тесты не доставлены
- [x] проверка группы: tests/runtime/test_pipeline_resume.py::test_p9_precedes_all_semantic_proof_reads (kind: integration), tests/runtime/test_pipeline_resume.py::test_semantic_comparison_order_and_single_live_config_instance (kind: integration), tests/runtime/test_pipeline_resume.py::test_semantic_drift_has_no_resume_or_mutation_effects (kind: integration), tests/runtime/test_pipeline_resume.py::test_repeated_semantic_failure_creates_no_artifacts (kind: integration), tests/runtime/test_pipeline_resume.py::test_semantic_failure_order_and_effect_boundary (kind: integration) зелёные на BEH-09, BEH-10, BEH-11, BEH-17, BEH-20

**Traces to:** [FR-08, FR-09, FR-13, FR-10, FR-17]

### TASK-005: Legacy возобновляется только по явной доказуемой процедуре
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-16.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-16
**Depends on:** [TASK-004]

**Checklist:**
- [x] реализовать BEH-16: Legacy возобновляется только по явной доказуемой процедуре
- [x] проверка группы: tests/integration/test_pipeline_e2e.py::test_legacy_semantics_require_explicit_provable_version (kind: integration) зелёные на BEH-16

**Traces to:** [FR-16]

### TASK-006: Формы pair и document остаются взаимоисключающими
P2 | ✅ DONE   Est: 0.5d — tdd-waiver/v1 (red-unverifiable: поведение доставлено TASK-003/TASK-004; санкция владельца 2026-09-04; регрессия test_pipeline_forms_remain_exclusive_and_kind_change_is_drift)

Реализовать сценарии BEH-18.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-18
**Depends on:** [TASK-005]

**Checklist:**
- [x] реализовать BEH-18: Формы pair и document остаются взаимоисключающими
- [x] проверка группы: tests/runtime/test_pipeline_config_kinds.py::test_pipeline_forms_remain_exclusive_and_kind_change_is_drift (kind: atp) зелёные на BEH-18

**Traces to:** [FR-18]

