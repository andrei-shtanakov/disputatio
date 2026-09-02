---
spec_stage: tasks
status: draft
version: 1
generated_by: fleet-agent
generated_at: 2026-09-03T03:43:14
source_prompt_version: ""
validation: ""
approved_by: ""
traces_to:
- behaviour-spec
upstream_hashes:
  behaviour-spec: cd5ac77984be516d3c2f6bd08f3858670daf0dd1
---

## Milestone 1: Immutable session semantics: resume сверяет неизменяемую половину [pipeline] со снапшотом fail-closed (disputatio#65)

Сгенерировано task_bridge из behaviour-spec бандла WS-disputatio-65 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Draft: исполнение только после человеческого approve.

### TASK-001: Run атомарно фиксирует версионированную immutable-модель
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-01.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-01

**Checklist:**
- [ ] реализовать BEH-01: Run атомарно фиксирует версионированную immutable-модель
- [ ] проверка группы: tests/runtime/test_pipeline_semantic_proof.py::test_run_commits_versioned_proof_atomically (kind: integration) зелёные на BEH-01

**Traces to:** [FR-01]

### TASK-002: Эквивалентное TOML-представление даёт ту же модель (+5 смежных BEH)
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-02, BEH-03, BEH-04, BEH-05, BEH-06, BEH-07.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-02 (—BEH-07)
**Depends on:** [TASK-001]

**Checklist:**
- [ ] реализовать BEH-02: Эквивалентное TOML-представление даёт ту же модель
- [ ] реализовать BEH-03: Пути канонизируются без привязки к машине
- [ ] реализовать BEH-04: Полная семантика pair-чеклистов неизменяема
- [ ] реализовать BEH-05: Полная семантика document-чеклиста неизменяема
- [ ] реализовать BEH-06: Все свойства и порядок дополнительных gates неизменяемы
- [ ] реализовать BEH-07: Четыре mutable control не создают drift
- [ ] проверка группы: tests/runtime/test_pipeline_semantics.py::test_equivalent_toml_and_explicit_defaults_have_one_projection (kind: contract), tests/runtime/test_pipeline_semantics.py::test_document_paths_are_relative_posix_and_machine_independent (kind: contract), tests/runtime/test_pipeline_semantics.py::test_pair_checklist_semantics_are_immutable (kind: atp), tests/runtime/test_pipeline_semantics.py::test_document_checklist_semantics_are_immutable (kind: atp), tests/runtime/test_pipeline_semantics.py::test_gate_order_and_all_properties_are_immutable (kind: atp), tests/runtime/test_pipeline_semantics.py::test_mutable_controls_apply_without_semantic_drift (kind: atp) зелёные на BEH-02, BEH-03, BEH-04, BEH-05, BEH-06, BEH-07

**Traces to:** [FR-02, FR-17, FR-03, FR-04, FR-10, FR-05, FR-06]

### TASK-003: Неизвестный pipeline-ключ отвергается закрытой схемой
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-08.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-08
**Depends on:** [TASK-002]

**Checklist:**
- [ ] реализовать BEH-08: Неизвестный pipeline-ключ отвергается закрытой схемой
- [ ] проверка группы: tests/runtime/test_pipeline_config.py::test_unknown_pipeline_keys_fail_closed_at_every_schema_level (kind: atp) зелёные на BEH-08

**Traces to:** [FR-07]

### TASK-004: P9 предшествует чтению ожидаемой семантики (+2 смежных BEH)
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-09, BEH-10, BEH-11.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-09 (—BEH-11)
**Depends on:** [TASK-003]

**Checklist:**
- [ ] реализовать BEH-09: P9 предшествует чтению ожидаемой семантики
- [ ] реализовать BEH-10: Semantic comparison занимает единственную раннюю позицию resume
- [ ] реализовать BEH-11: Drift останавливает resume без побочных эффектов
- [ ] проверка группы: tests/runtime/test_pipeline_resume.py::test_p9_precedes_all_semantic_proof_reads (kind: integration), tests/runtime/test_pipeline_resume.py::test_semantic_comparison_order_and_single_live_config_instance (kind: integration), tests/runtime/test_pipeline_resume.py::test_semantic_drift_has_no_resume_or_mutation_effects (kind: integration) зелёные на BEH-09, BEH-10, BEH-11

**Traces to:** [FR-08, FR-09, FR-13, FR-10]

### TASK-005: Недоказуемая семантика запрещает продолжение без fallback (+1 смежных BEH)
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-12, BEH-13.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-12 (—BEH-13)
**Depends on:** [TASK-004]

**Checklist:**
- [ ] реализовать BEH-12: Недоказуемая семантика запрещает продолжение без fallback
- [ ] реализовать BEH-13: Каждый источник доказательства проверяется и согласуется
- [ ] проверка группы: tests/runtime/test_pipeline_semantic_proof.py::test_unprovable_semantics_fail_closed_without_fallback (kind: atp), tests/runtime/test_pipeline_semantic_proof.py::test_all_proof_sources_require_integrity_and_consistency (kind: atp) зелёные на BEH-12, BEH-13

**Traces to:** [FR-11, FR-12]

### TASK-006: Drift-диагностика точна и не раскрывает чувствительные значения
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-14.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-14
**Depends on:** [TASK-005]

**Checklist:**
- [ ] реализовать BEH-14: Drift-диагностика точна и не раскрывает чувствительные значения
- [ ] проверка группы: tests/runtime/test_pipeline_semantics.py::test_drift_diagnostic_is_sorted_specific_and_redacted (kind: contract) зелёные на BEH-14

**Traces to:** [FR-14]

### TASK-007: Ошибка доказательства различает безопасные причины
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-15.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-15
**Depends on:** [TASK-006]

**Checklist:**
- [ ] реализовать BEH-15: Ошибка доказательства различает безопасные причины
- [ ] проверка группы: tests/runtime/test_pipeline_semantic_proof.py::test_proof_errors_are_distinct_safe_and_actionable (kind: contract) зелёные на BEH-15

**Traces to:** [FR-15]

### TASK-008: Legacy возобновляется только по явной доказуемой процедуре
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-16.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-16
**Depends on:** [TASK-007]

**Checklist:**
- [ ] реализовать BEH-16: Legacy возобновляется только по явной доказуемой процедуре
- [ ] проверка группы: tests/integration/test_pipeline_e2e.py::test_legacy_semantics_require_explicit_provable_version (kind: integration) зелёные на BEH-16

**Traces to:** [FR-16]

### TASK-009: Повтор после semantic-отказа не видит новых артефактов
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-17.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-17
**Depends on:** [TASK-008]

**Checklist:**
- [ ] реализовать BEH-17: Повтор после semantic-отказа не видит новых артефактов
- [ ] проверка группы: tests/runtime/test_pipeline_resume.py::test_repeated_semantic_failure_creates_no_artifacts (kind: integration) зелёные на BEH-17

**Traces to:** [FR-17]

### TASK-010: Формы pair и document остаются взаимоисключающими
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-18.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-18
**Depends on:** [TASK-009]

**Checklist:**
- [ ] реализовать BEH-18: Формы pair и document остаются взаимоисключающими
- [ ] проверка группы: tests/runtime/test_pipeline_config_kinds.py::test_pipeline_forms_remain_exclusive_and_kind_change_is_drift (kind: atp) зелёные на BEH-18

**Traces to:** [FR-18]

### TASK-011: Каждая строка immutable-классификации имеет регрессионный пример
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-19.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-19
**Depends on:** [TASK-010]

**Checklist:**
- [ ] реализовать BEH-19: Каждая строка immutable-классификации имеет регрессионный пример
- [ ] проверка группы: tests/runtime/test_pipeline_semantics.py::test_every_immutable_classification_row_is_enforced (kind: atp) зелёные на BEH-19

**Traces to:** [FR-02, FR-03, FR-04, FR-05, FR-18]

### TASK-012: Отказ semantic comparison сохраняет действующие контракты
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-20.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-20
**Depends on:** [TASK-011]

**Checklist:**
- [ ] реализовать BEH-20: Отказ semantic comparison сохраняет действующие контракты
- [ ] проверка группы: tests/runtime/test_pipeline_resume.py::test_semantic_failure_order_and_effect_boundary (kind: integration) зелёные на BEH-20

**Traces to:** [FR-08, FR-09, FR-10, FR-13]

### TASK-013: Классификация и исполнимый контракт документированы единообразно
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-21.
Source: workstreams/WS-disputatio-65/spec/15-behaviour-spec.md#BEH-21
**Depends on:** [TASK-012]

**Checklist:**
- [ ] реализовать BEH-21: Классификация и исполнимый контракт документированы единообразно
- [ ] проверка группы: tests/runtime/test_pipeline_semantics.py::test_schema_parser_and_canonicalizer_classifications_match (kind: manual) зелёные на BEH-21

**Traces to:** [FR-07, FR-19]

