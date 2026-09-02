---
traces_to:
- design
spec_stage: tasks
status: approved
version: 2
generated_by: fleet-agent
generated_at: '2026-09-02T08:04:02'
source_prompt_version: ''
validation: warn
approved_by: verifier-tests
approved_at: '2026-09-02T06:03:08Z'
---

## Milestone 1: Построчный разбор без состояния: _changed_lines со state-парсером + перехват UnicodeDecodeError по конвенции (disputatio#57)

Сгенерировано task_bridge из behaviour-spec бандла WS-disputatio-57 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Draft: исполнение только после человеческого approve.

### TASK-001: Строки изменения учитываются только после заголовка ханка
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-01.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-01

**Checklist:**
- [x] реализовать BEH-01: Строки изменения учитываются только после заголовка ханка
- [x] проверка группы: tests/core/test_oscillation.py::test_changed_lines_requires_open_hunk (kind: atp) зелёные на BEH-01

**Traces to:** [FR-01, FR-02, FR-06]

### TASK-002: Заголовок следующего файла закрывает открытый ханк
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-02.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-02
**Depends on:** [TASK-001]

**Checklist:**
- [x] реализовать BEH-02: Заголовок следующего файла закрывает открытый ханк
- [x] проверка группы: tests/core/test_oscillation.py::test_changed_lines_tracks_multiple_files_and_hunks (kind: atp) зелёные на BEH-02

**Traces to:** [FR-02, FR-07]

### TASK-003: Новый заголовок ханка продолжает разбор того же файла
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-03.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-03
**Depends on:** [TASK-002]
Закрыто waiver-ом владельца (tdd-waiver/v1,
spec/.tdd-evidence/waivers/a5b6a41bc40a1c96/TASK-003.json): BEH-03 уже
обеспечен реализацией TASK-001/002, честный RED невозможен; регрессионный
тест добавлен зелёным, вне TDD-цикла.

**Checklist:**
- [x] реализовать BEH-03: Новый заголовок ханка продолжает разбор того же файла
- [x] проверка группы: tests/core/test_oscillation.py::test_changed_lines_tracks_consecutive_hunks (kind: atp) зелёные на BEH-03

**Traces to:** [FR-02, FR-07]

### TASK-004: Служебные и контекстные строки исключаются
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-04.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-04
**Depends on:** [TASK-003]

Закрыто батч-waiver-ом владельца 2026-09-02 (tdd-waiver/v1,
spec/.tdd-evidence/waivers/a5b6a41bc40a1c96/TASK-004.json):
поведение уже обеспечено TASK-001/002, честный RED невозможен;
регрессия — зелёный тест test_changed_lines_excludes_metadata_context_and_no_newline_marker.

**Checklist:**
- [x] реализовать BEH-04: Служебные и контекстные строки исключаются
- [x] проверка группы: tests/core/test_oscillation.py::test_changed_lines_excludes_metadata_context_and_no_newline_marker (kind: atp) зелёные на BEH-04

**Traces to:** [FR-03]

### TASK-005: Похожее на метаданные содержимое добавленной строки сохраняется
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-05.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-05
**Depends on:** [TASK-004]

**Checklist:**
- [x] реализовать BEH-05: Похожее на метаданные содержимое добавленной строки сохраняется
- [x] проверка группы: tests/core/test_oscillation.py::test_changed_lines_preserves_added_metadata_like_content (kind: atp) зелёные на BEH-05

**Traces to:** [FR-04, FR-05]

### TASK-006: Похожее на метаданные содержимое удалённой строки сохраняется
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-06.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-06
**Depends on:** [TASK-005]

Закрыто waiver-ом владельца 2026-09-02 (tdd-waiver/v1,
spec/.tdd-evidence/waivers/a5b6a41bc40a1c96/TASK-006.json): фикс
TASK-005 накрыл парный кейс, честный RED невозможен; регрессия —
зелёный тест test_changed_lines_preserves_removed_metadata_like_content.

**Checklist:**
- [x] реализовать BEH-06: Похожее на метаданные содержимое удалённой строки сохраняется
- [x] проверка группы: tests/core/test_oscillation.py::test_changed_lines_preserves_removed_metadata_like_content (kind: atp) зелёные на BEH-06

**Traces to:** [FR-04, FR-05]

### TASK-007: Нормализация ограничена маркером и хвостовыми пробелами
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-07.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-07
**Depends on:** [TASK-006]

Закрыто батч-waiver-ом владельца 2026-09-02 (tdd-waiver/v1,
spec/.tdd-evidence/waivers/a5b6a41bc40a1c96/TASK-007.json):
поведение уже обеспечено TASK-001/002, честный RED невозможен;
регрессия — зелёный тест test_changed_lines_normalizes_to_unique_content_set.

**Checklist:**
- [x] реализовать BEH-07: Нормализация ограничена маркером и хвостовыми пробелами
- [x] проверка группы: tests/core/test_oscillation.py::test_changed_lines_normalizes_to_unique_content_set (kind: atp) зелёные на BEH-07

**Traces to:** [FR-05]

### TASK-008: Служебные различия патчей не меняют похожесть
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-08.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-08
**Depends on:** [TASK-007]

Закрыто батч-waiver-ом владельца 2026-09-02 (tdd-waiver/v1,
spec/.tdd-evidence/waivers/a5b6a41bc40a1c96/TASK-008.json):
поведение уже обеспечено TASK-001/002, честный RED невозможен;
регрессия — зелёный тест test_patch_similarity_ignores_all_service_line_differences.

**Checklist:**
- [x] реализовать BEH-08: Служебные различия патчей не меняют похожесть
- [x] проверка группы: tests/core/test_oscillation.py::test_patch_similarity_ignores_all_service_line_differences (kind: contract) зелёные на BEH-08

**Traces to:** [FR-08, FR-09]

### TASK-009: Формула Жаккара и пороги детектора остаются прежними
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-09.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-09
**Depends on:** [TASK-008]

Закрыто батч-waiver-ом владельца 2026-09-02 (tdd-waiver/v1,
spec/.tdd-evidence/waivers/a5b6a41bc40a1c96/TASK-009.json):
поведение уже обеспечено TASK-001/002, честный RED невозможен;
регрессия — зелёный тест test_stateful_changed_lines_preserves_oscillation_contract.

**Checklist:**
- [x] реализовать BEH-09: Формула Жаккара и пороги детектора остаются прежними
- [x] проверка группы: tests/core/test_oscillation.py::test_stateful_changed_lines_preserves_oscillation_contract (kind: integration) зелёные на BEH-09

**Traces to:** [FR-08]

### TASK-010: Вызовы разбора детерминированы и изолированы
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-10.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-10
**Depends on:** [TASK-009]

Закрыто батч-waiver-ом владельца 2026-09-02 (tdd-waiver/v1,
spec/.tdd-evidence/waivers/a5b6a41bc40a1c96/TASK-010.json):
поведение уже обеспечено TASK-001/002, честный RED невозможен;
регрессия — зелёный тест test_changed_lines_has_no_state_between_calls.

**Checklist:**
- [x] реализовать BEH-10: Вызовы разбора детерминированы и изолированы
- [x] проверка группы: tests/core/test_oscillation.py::test_changed_lines_has_no_state_between_calls (kind: contract) зелёные на BEH-10

**Traces to:** [FR-01, FR-02, FR-05]

### TASK-011: Невалидный UTF-8 преобразуется в SyntaxError с диагностикой
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-11.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-11
**Depends on:** [TASK-010]

**Checklist:**
- [ ] реализовать BEH-11: Невалидный UTF-8 преобразуется в SyntaxError с диагностикой
- [ ] проверка группы: tests/runtime/test_core_purity.py::test_scan_package_purity_wraps_unicode_decode_error (kind: integration) зелёные на BEH-11

**Traces to:** [FR-10, FR-11]

### TASK-012: Ошибка декодирования атомарно прекращает весь scan
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-12.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-12
**Depends on:** [TASK-011]

**Checklist:**
- [ ] реализовать BEH-12: Ошибка декодирования атомарно прекращает весь scan
- [ ] проверка группы: tests/runtime/test_core_purity.py::test_scan_package_purity_fails_atomically_on_invalid_utf8 (kind: integration) зелёные на BEH-12

**Traces to:** [FR-10, FR-12]

### TASK-013: Перехват не маскирует другие ошибки
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-13.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-13
**Depends on:** [TASK-012]

**Checklist:**
- [ ] реализовать BEH-13: Перехват не маскирует другие ошибки
- [ ] проверка группы: tests/runtime/test_core_purity.py::test_scan_package_purity_only_wraps_unicode_decode_error (kind: contract) зелёные на BEH-13

**Traces to:** [FR-13]

### TASK-014: Публичные сигнатуры и границы зависимостей сохраняются
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-14.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-14
**Depends on:** [TASK-013]

**Checklist:**
- [ ] реализовать BEH-14: Публичные сигнатуры и границы зависимостей сохраняются
- [ ] проверка группы: tests/core/test_purity.py::test_core_import_boundary (kind: integration) зелёные на BEH-14

**Traces to:** [FR-08, FR-13]

### TASK-015: Документация сообщает новые граничные контракты
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-15.
Source: workstreams/WS-disputatio-57/spec/15-behaviour-spec.md#BEH-15
**Depends on:** [TASK-014]

**Checklist:**
- [ ] реализовать BEH-15: Документация сообщает новые граничные контракты
- [ ] проверка группы: tests/runtime/test_core_purity.py::test_scan_package_purity_documents_invalid_utf8_contract (kind: manual) зелёные на BEH-15

**Traces to:** [FR-14]

