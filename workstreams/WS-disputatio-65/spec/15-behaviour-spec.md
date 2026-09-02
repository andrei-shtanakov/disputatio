---
spec_stage: behaviour-spec
status: draft
owner_role: product
traces_to: [requirements]
upstream_hashes: {requirements: "075400dd9e93a76cd5b8fdf12359422eb9b68e9e"}
---

# Behaviour spec: неизменяемая семантика сессий при resume пайплайна

## Область поведения

Сценарии определяют наблюдаемый контракт `disp pipeline run` и `disp pipeline resume` для видов `pair` и `document`: создание удостоверенного доказательства, каноническое сравнение закрытой immutable-проекции, разрешённые mutable controls и fail-closed поведение до любых действий сессии или рабочего дерева. Существующие FSM, baseline-гейты и правила reconciliation остаются неизменными.

#### BEH-01: Run атомарно фиксирует версионированную immutable-модель

`traces: [FR-01]`

- **Дано**: полностью валидный конфиг нового пайплайна `pair` или `document`, включая значения по умолчанию и разрешённые чеклисты.
- **Когда**: `disp pipeline run` доходит до durable commit point перед первой сессией.
- **Тогда**: в удостоверенном P9 durable-состоянии появляется криптографически связанное доказательство итоговой immutable-проекции и версии её канонизации.
- **И**: crash до commit point не оставляет частичного доказательства, принимаемого `resume`, а после commit point доказательство целиком проверяемо.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/runtime/test_pipeline_semantic_proof.py::test_run_commits_versioned_proof_atomically`

#### BEH-02: Эквивалентное TOML-представление даёт ту же модель

`traces: [FR-02, FR-17]`

- **Дано**: исходный и живой конфиги различаются комментариями, пробелами, кавычками, порядком незначимых TOML-таблиц и явной записью значения, равного default.
- **Когда**: `resume` канонизирует обе разобранные модели записанной поддерживаемой версией.
- **Тогда**: immutable-проекции и их digest совпадают на всех поддерживаемых платформах, и semantic comparison разрешает продолжение.
- **И**: повтор того же `resume` даёт тот же результат.
- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_equivalent_toml_and_explicit_defaults_have_one_projection`

#### BEH-03: Пути канонизируются без привязки к машине

`traces: [FR-03]`

- **Дано**: валидные относительные пути документов для `pair` и `document` на платформах с разными разделителями и разными абсолютными workspace root.
- **Когда**: строится immutable-проекция.
- **Тогда**: пути представлены валидированными относительными POSIX-путями без `resolve()`, workspace root и иных машинных префиксов.
- **И**: два различных относительных нормативных пути не считаются равными только потому, что указывают на один файл.
- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_document_paths_are_relative_posix_and_machine_independent`

#### BEH-04: Полная семантика pair-чеклистов неизменяема

`traces: [FR-04, FR-10]`

- **Дано**: существующий `pair` с разрешёнными после vendor override чеклистами `spec` и `pair`.
- **Когда**: живой конфиг отдельно добавляет, удаляет, переименовывает или переставляет пункт либо меняет его текст или `findings_item`.
- **Тогда**: каждое изменение обнаруживается как semantic drift и `resume` завершается ненулевым кодом.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_pair_checklist_semantics_are_immutable`

#### BEH-05: Полная семантика document-чеклиста неизменяема

`traces: [FR-04, FR-10]`

- **Дано**: существующий `document` с операторским `doc.items` и `doc.findings_item`.
- **Когда**: живой конфиг отдельно меняет id, текст, порядок или состав пунктов либо назначенный `findings_item`.
- **Тогда**: каждое изменение обнаруживается как semantic drift до продолжения пайплайна.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_document_checklist_semantics_are_immutable`

#### BEH-06: Все свойства и порядок дополнительных gates неизменяемы

`traces: [FR-05, FR-10]`

- **Дано**: удостоверенная модель содержит упорядоченный список дополнительных gates.
- **Когда**: живой конфиг добавляет, удаляет или переставляет gate либо меняет его `name`, `cmd` или `enabled`.
- **Тогда**: каждое отличие является drift; ни один дополнительный или baseline-gate не исполняется.
- **И**: baseline-гейты не становятся отключаемыми через semantic proof.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_gate_order_and_all_properties_are_immutable`

#### BEH-07: Четыре mutable control не создают drift

`traces: [FR-06]`

- **Дано**: immutable-проекция живого конфига совпадает с ожидаемой.
- **Когда**: отдельно и совместно изменены `soft_max_pipeline_tokens`, `soft_max_pipeline_wall_seconds`, `protected_branches` и `anchor_path`, причём новый `anchor_path` валиден и проходит P9.
- **Тогда**: semantic comparison разрешает продолжение, а новые controls управляют текущими и последующими действиями без переписывания истории.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_mutable_controls_apply_without_semantic_drift`

#### BEH-08: Неизвестный pipeline-ключ отвергается закрытой схемой

`traces: [FR-07]`

- **Дано**: конфиг содержит неизвестный ключ в `[pipeline]`, `pipeline.checklists`, конкретном чеклисте, его `items` или записи `pipeline.gates`.
- **Когда**: общий загрузчик разбирает конфиг для `run` или `resume`.
- **Тогда**: каждый вариант завершается `ConfigError` до построения доказательства или продолжения; неизвестное поле не становится mutable по умолчанию.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_config.py::test_unknown_pipeline_keys_fail_closed_at_every_schema_level`

#### BEH-09: P9 предшествует чтению ожидаемой семантики

`traces: [FR-08, FR-09]`

- **Дано**: вызов `resume` с живыми `anchor_path` и `slug`, а порты чтения манифеста, снапшотов и proof наблюдаемы.
- **Когда**: выполняется шаг 0.
- **Тогда**: P9 успешно удостоверяет identity до первого чтения любого источника ожидаемой модели.
- **И**: при отказе P9 ни манифест, ни снапшоты, ни proof не читаются для semantic comparison.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/runtime/test_pipeline_resume.py::test_p9_precedes_all_semantic_proof_reads`

#### BEH-10: Semantic comparison занимает единственную раннюю позицию resume

`traces: [FR-09, FR-13]`

- **Дано**: P9 успешен и доверенный манифест прочитан, а вызовы semantic proof, `detect_parked`, worktree classification и runner записываются spy-портами.
- **Когда**: выполняется `resume`.
- **Тогда**: целостность proof проверяется и модели сравниваются сразу после чтения манифеста, до parked detection, чтения изменённых документов и классификации дерева.
- **И**: тот же экземпляр живого `PipelineConfig` используется сравнением и всеми последующими действиями без повторного чтения файла.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/runtime/test_pipeline_resume.py::test_semantic_comparison_order_and_single_live_config_instance`

#### BEH-11: Drift останавливает resume без побочных эффектов

`traces: [FR-10]`

- **Дано**: живая и ожидаемая immutable-проекции различаются хотя бы в одном поле.
- **Когда**: `resume` выполняет semantic comparison.
- **Тогда**: команда завершается ненулевым кодом и не запускает и не возобновляет сессию, не выполняет gate, не применяет решение или replay intent, не вызывает reset/clean и не пишет манифест, snapshot или baseline.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/runtime/test_pipeline_resume.py::test_semantic_drift_has_no_resume_or_mutation_effects`

#### BEH-12: Недоказуемая семантика запрещает продолжение без fallback

`traces: [FR-11]`

- **Дано**: proof по отдельности отсутствует, повреждён, неподтверждён, не разбирается, имеет неподдерживаемую версию или внутренне противоречив.
- **Когда**: `resume` пытается восстановить ожидаемую модель.
- **Тогда**: каждый случай fail-closed без fallback на живой конфиг или текущую версию кода и без автоматической записи либо обновления proof.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_semantic_proof.py::test_unprovable_semantics_fail_closed_without_fallback`

#### BEH-13: Каждый источник доказательства проверяется и согласуется

`traces: [FR-12]`

- **Дано**: ожидаемая модель зависит от нескольких удостоверенных артефактов, включая манифест, `config.toml`, `checklists.toml` или snapshot.
- **Когда**: один из источников отсутствует, имеет неверный digest, недопустимую схему или расходится с остальными.
- **Тогда**: `resume` отказывает, даже если живая модель совпадает с другим источником; содержимое повреждённого источника не используется.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_semantic_proof.py::test_all_proof_sources_require_integrity_and_consistency`

#### BEH-14: Drift-диагностика точна и не раскрывает чувствительные значения

`traces: [FR-14]`

- **Дано**: drift одновременно затрагивает безопасный путь документа, gate command и текст чеклиста.
- **Когда**: `resume` формирует ошибку.
- **Тогда**: сообщение называет pipeline/slug, версию канонизации и отсортированные канонические пути всех отличий.
- **И**: полные команды gates и тексты prompt/checklist, а также иные чувствительные значения не печатаются; старое и новое значения выводятся только для безопасных enum, чисел и относительных путей документов.
- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_drift_diagnostic_is_sorted_specific_and_redacted`

#### BEH-15: Ошибка доказательства различает безопасные причины

`traces: [FR-15]`

- **Дано**: по отдельности возникают отсутствие, нарушение digest или связи, ошибка разбора, противоречие и неподдерживаемая версия доказательства.
- **Когда**: `resume` сообщает отказ оператору.
- **Тогда**: диагностика различает причины, называет проблемный артефакт без его содержимого и предлагает восстановить удостоверенные данные либо завершить или пересоздать пайплайн.
- **И**: принять живой конфиг как новый baseline не предлагается.
- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/runtime/test_pipeline_semantic_proof.py::test_proof_errors_are_distinct_safe_and_actionable`

#### BEH-16: Legacy возобновляется только по явной доказуемой процедуре

`traces: [FR-16]`

- **Дано**: fixtures каждой поддерживаемой legacy-версии и legacy-манифест без достаточных удостоверенных immutable-данных.
- **Когда**: выполняется `resume`.
- **Тогда**: поддерживаемая версия восстанавливает ожидаемую модель только своей явной процедурой из сохранённых после P9 данных.
- **И**: недостаточная версия fail-closed без in-place миграции, автодобавления proof и предположения эквивалентности по одному `documents.kind`.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/integration/test_pipeline_e2e.py::test_legacy_semantics_require_explicit_provable_version`

#### BEH-17: Повтор после semantic-отказа не видит новых артефактов

`traces: [FR-17]`

- **Дано**: первый `resume` завершился из-за drift или недоказуемой семантики.
- **Когда**: оператор повторяет тот же вызов без восстановления удостоверенных данных.
- **Тогда**: результат и причина semantic comparison повторяются, а новый snapshot, digest, proof или baseline от первого отказа отсутствует.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/runtime/test_pipeline_resume.py::test_repeated_semantic_failure_creates_no_artifacts`

#### BEH-18: Формы pair и document остаются взаимоисключающими

`traces: [FR-18]`

- **Дано**: конфиги полной формы `pair`, полной формы `document`, смешанной и неполной формы, включая `max_architectural_returns` у `document`.
- **Когда**: конфиг валидируется для `run` или `resume`.
- **Тогда**: допустимы только `pair` со `spec_path` и `plan_path` и `document` с `document_path` и операторским doc-чеклистом; смешанные, неполные и запрещённые поля дают `ConfigError`.
- **И**: смена допустимой формы уже существующего пайплайна диагностируется как drift, а не миграция.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_config_kinds.py::test_pipeline_forms_remain_exclusive_and_kind_change_is_drift`

#### BEH-19: Каждая строка immutable-классификации имеет регрессионный пример

`traces: [FR-02, FR-03, FR-04, FR-05, FR-18]`

- **Дано**: табличные cases для каждого immutable-поля обоих видов, всех коллекций, свойств gates и применяемых defaults.
- **Когда**: каждый case сохраняется как baseline, затем сравнивается без изменения и с одним изменённым значением.
- **Тогда**: неизменённый case проходит, а изменённый даёт drift; сериализация и digest детерминированы.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_every_immutable_classification_row_is_enforced`

#### BEH-20: Отказ semantic comparison сохраняет действующие контракты

`traces: [FR-08, FR-09, FR-10, FR-13]`

- **Дано**: spy-порты охватывают P9, manifest/proof reads, parked detection, reconciliation, session runner, gates, operator intent и все мутирующие Git/manifest операции.
- **Когда**: отдельно воспроизводятся drift и каждая причина недоказуемой семантики, включая crash-injection на границах чтения.
- **Тогда**: наблюдается порядок `P9 → manifest → semantic proof/comparison`, после отказа дальнейших вызовов нет, рабочее дерево, индекс и durable artifacts побайтно неизменны.
- **И**: единственное разрешённое более раннее изменение — нормативная реакция P9 на подтверждённую подмену control plane; FSM обоих видов, baseline gates и reconciliation не ослаблены.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/runtime/test_pipeline_resume.py::test_semantic_failure_order_and_effect_boundary`

#### BEH-21: Классификация и исполнимый контракт документированы единообразно

`traces: [FR-07, FR-19]`

- **Дано**: реализация и тестовое доказательство immutable semantics завершены.
- **Когда**: QA сверяет SPEC-002, операторскую документацию, докстринги загрузки чеклистов и снапшотов и декларативную схему реализации.
- **Тогда**: источники одинаково описывают закрытую классификацию, порядок `resume`, legacy-политику и безопасную диагностику; добавление parser/dataclass-поля без классификации ломает тест.
- **И**: ограничение issue #65 удалено, а документация не полагается на обещание неизменного операторского конфига.
- **checked_by**: `status: planned` `kind: manual` `owner: qa` `target: tests/runtime/test_pipeline_semantics.py::test_schema_parser_and_canonicalizer_classifications_match`

## Матрица трассируемости

| Сценарий | Требования |
|---|---|
| BEH-01 | FR-01 |
| BEH-02 | FR-02, FR-17 |
| BEH-03 | FR-03 |
| BEH-04 | FR-04, FR-10 |
| BEH-05 | FR-04, FR-10 |
| BEH-06 | FR-05, FR-10 |
| BEH-07 | FR-06 |
| BEH-08 | FR-07 |
| BEH-09 | FR-08, FR-09 |
| BEH-10 | FR-09, FR-13 |
| BEH-11 | FR-10 |
| BEH-12 | FR-11 |
| BEH-13 | FR-12 |
| BEH-14 | FR-14 |
| BEH-15 | FR-15 |
| BEH-16 | FR-16 |
| BEH-17 | FR-17 |
| BEH-18 | FR-18 |
| BEH-19 | FR-02, FR-03, FR-04, FR-05, FR-18 |
| BEH-20 | FR-08, FR-09, FR-10, FR-13 |
| BEH-21 | FR-07, FR-19 |

Все FR-01–FR-19 покрыты как минимум одним сценарием BEH-01–BEH-21.
