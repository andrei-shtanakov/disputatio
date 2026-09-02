---
spec_stage: behaviour-spec
status: draft
owner_role: product
traces_to: [requirements]
upstream_hashes: {requirements: "e18843bef8d5fc3860f91c7a38adeb705a544ba7"}
---

# Behaviour spec: построчный разбор без состояния между вызовами и единый отказ на не-UTF-8

## Область поведения

Сценарии фиксируют наблюдаемое поведение двух границ: выделение изменённых строк из unified diff функцией `_changed_lines` и обработку не декодируемых как UTF-8 Python-файлов функцией `scan_package_purity`. Каждый сценарий независим: результат предыдущего вызова не является предусловием следующего.

#### BEH-01: Строки изменения учитываются только после заголовка ханка

`traces: [FR-01, FR-02, FR-06]`

- **Дано**: patch содержит строки `+` и `-` до первого заголовка `@@`, а затем ханк с добавленной и удалённой строками.
- **Когда**: вызывается `_changed_lines`.
- **Тогда**: результат содержит только содержимое добавленной и удалённой строк открытого ханка; неподтверждённые строки до ханка проигнорированы.
- **И**: пустой patch и patch без `@@` возвращают пустое множество.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/core/test_oscillation.py::test_changed_lines_requires_open_hunk`

#### BEH-02: Заголовок следующего файла закрывает открытый ханк

`traces: [FR-02, FR-07]`

- **Дано**: многофайловый patch содержит ханк первого файла, затем `diff --git `, строки файловых метаданных и ханк второго файла.
- **Когда**: вызывается `_changed_lines`.
- **Тогда**: изменения обоих ханков объединены в одно множество.
- **И**: строки между `diff --git ` и следующим `@@` не интерпретируются как изменения.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/core/test_oscillation.py::test_changed_lines_tracks_multiple_files_and_hunks`

#### BEH-03: Новый заголовок ханка продолжает разбор того же файла

`traces: [FR-02, FR-07]`

- **Дано**: patch одного файла содержит два заголовка `@@` и строки изменения после каждого из них.
- **Когда**: вызывается `_changed_lines`.
- **Тогда**: строки изменения из обоих ханков входят в результат в соответствии с set-семантикой.
- **И**: конец patch не добавляет отдельного значения и корректно завершает открытый ханк.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/core/test_oscillation.py::test_changed_lines_tracks_consecutive_hunks`

#### BEH-04: Служебные и контекстные строки исключаются

`traces: [FR-03]`

- **Дано**: patch содержит Git extended headers вне ханка, файловые заголовки `--- ` и `+++ `, заголовок `@@`, контекстную строку с начальным пробелом и маркер `\ No newline at end of file`.
- **Когда**: вызывается `_changed_lines`.
- **Тогда**: ни одна из перечисленных служебных или контекстных строк не входит в результат.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/core/test_oscillation.py::test_changed_lines_excludes_metadata_context_and_no_newline_marker`

#### BEH-05: Похожее на метаданные содержимое добавленной строки сохраняется

`traces: [FR-04, FR-05]`

- **Дано**: внутри открытого ханка находятся добавленные строки, содержимое которых после первого `+` начинается с `+`, `++`, `+++ `, `-`, `--`, `--- `, `@@` и `diff --git `.
- **Когда**: вызывается `_changed_lines`.
- **Тогда**: удалён ровно один первый маркер `+`, а всё оставшееся содержимое каждой строки сохранено и включено в множество.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/core/test_oscillation.py::test_changed_lines_preserves_added_metadata_like_content`

#### BEH-06: Похожее на метаданные содержимое удалённой строки сохраняется

`traces: [FR-04, FR-05]`

- **Дано**: внутри открытого ханка находятся удалённые строки, содержимое которых после первого `-` начинается с `+`, `++`, `+++ `, `-`, `--`, `--- `, `@@` и `diff --git `.
- **Когда**: вызывается `_changed_lines`.
- **Тогда**: удалён ровно один первый маркер `-`, а всё оставшееся содержимое каждой строки сохранено и включено в множество.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/core/test_oscillation.py::test_changed_lines_preserves_removed_metadata_like_content`

#### BEH-07: Нормализация ограничена маркером и хвостовыми пробелами

`traces: [FR-05]`

- **Дано**: ханк содержит повторяющиеся строки изменения с хвостовыми пробельными символами, начальными пробелами содержимого, различным регистром и строку из одного diff-маркера.
- **Когда**: вызывается `_changed_lines`.
- **Тогда**: с каждой строки удалены ровно один diff-маркер и результат `rstrip()`.
- **И**: начальные и внутренние пробелы и регистр сохранены, дубликаты схлопнуты, а пустая строка присутствует как допустимый элемент множества.
- **checked_by**: `status: planned` `kind: atp` `owner: qa` `target: tests/core/test_oscillation.py::test_changed_lines_normalizes_to_unique_content_set`

#### BEH-08: Служебные различия патчей не меняют похожесть

`traces: [FR-08, FR-09]`

- **Дано**: два patch имеют одинаковое нормализованное множество строк изменения, но различаются путями, индексами, mode-записями, timestamps, файловыми заголовками, порядком и объёмом служебных строк.
- **Когда**: для каждого patch вычисляются `_changed_lines` и `patch_similarity` относительно одного и того же третьего patch.
- **Тогда**: множества двух patch равны и оба коэффициента похожести равны.
- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/core/test_oscillation.py::test_patch_similarity_ignores_all_service_line_differences`

#### BEH-09: Формула Жаккара и пороги детектора остаются прежними

`traces: [FR-08]`

- **Дано**: пары patch с известными пересечением и объединением множеств изменений, включая пару с двумя пустыми множествами.
- **Когда**: вызывается `patch_similarity` и затем существующий сценарий принятия решения об осцилляции.
- **Тогда**: коэффициент равен размеру пересечения, делённому на размер объединения, а для двух пустых множеств равен `1.0`.
- **И**: значения `OSCILLATION_DIFF_THRESHOLD` и `CLAIM_SIMILARITY_THRESHOLD`, сравнение claim и порядок решения об осцилляции не изменены.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/core/test_oscillation.py::test_stateful_changed_lines_preserves_oscillation_contract`

#### BEH-10: Вызовы разбора детерминированы и изолированы

`traces: [FR-01, FR-02, FR-05]`

- **Дано**: один patch с открытым ханком и другой patch без заголовка ханка.
- **Когда**: `_changed_lines` многократно вызывается для первого patch, затем для второго, затем снова для первого.
- **Тогда**: равные входы дают равные множества, второй patch даёт пустое множество, а состояние ханка не переносится между вызовами.
- **И**: исходные строки patch остаются неизменными.
- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/core/test_oscillation.py::test_changed_lines_has_no_state_between_calls`

#### BEH-11: Невалидный UTF-8 преобразуется в SyntaxError с диагностикой

`traces: [FR-10, FR-11]`

- **Дано**: рекурсивно обнаруженный `.py` файл содержит последовательность байтов, не декодируемую как UTF-8.
- **Когда**: `scan_package_purity` читает пакет.
- **Тогда**: наружу возбуждается `SyntaxError`, а не `UnicodeDecodeError`.
- **И**: сообщение `SyntaxError` содержит строковое представление пути проблемного файла.
- **И**: `SyntaxError.__cause__` является тем же экземпляром `UnicodeDecodeError`, который возник при чтении файла.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/runtime/test_core_purity.py::test_scan_package_purity_wraps_unicode_decode_error`

#### BEH-12: Ошибка декодирования атомарно прекращает весь scan

`traces: [FR-10, FR-12]`

- **Дано**: пакет содержит Python-файл с обнаруживаемым нарушением чистоты и другой `.py` файл с невалидным UTF-8 независимо от порядка обхода.
- **Когда**: вызывается `scan_package_purity`.
- **Тогда**: весь вызов завершается `SyntaxError` и не возвращает полный или частичный список `PurityViolation`.
- **И**: проблемный файл не пропускается, байты не заменяются и альтернативная кодировка не применяется.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/runtime/test_core_purity.py::test_scan_package_purity_fails_atomically_on_invalid_utf8`

#### BEH-13: Перехват не маскирует другие ошибки

`traces: [FR-13]`

- **Дано**: отдельно подготовлены UTF-8-декодируемый файл с синтаксической ошибкой, недоступный файл и чтение, возбуждающее иное исключение.
- **Когда**: каждый случай проходит через `scan_package_purity`.
- **Тогда**: синтаксическая ошибка продолжает выходить как `SyntaxError` AST-разбора, а файловые и прочие ошибки сохраняют действующие тип и поведение.
- **И**: новая ветвь преобразует только `UnicodeDecodeError`.
- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/runtime/test_core_purity.py::test_scan_package_purity_only_wraps_unicode_decode_error`

#### BEH-14: Публичные сигнатуры и границы зависимостей сохраняются

`traces: [FR-08, FR-13]`

- **Дано**: реализация state-парсера и обработки ошибки декодирования завершена.
- **Когда**: выполняются существующие проверки API, импортов ядра, детектора осцилляции и сканера чистоты.
- **Тогда**: сигнатуры `_changed_lines`, `patch_similarity` и `scan_package_purity`, структура `PurityViolation`, установленные пороги и запрет зависимостей `disputatio.core` от внешних слоёв сохранены.
- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/core/test_purity.py::test_core_import_boundary`

#### BEH-15: Документация сообщает новые граничные контракты

`traces: [FR-14]`

- **Дано**: пользователь читает документацию `_changed_lines` и `scan_package_purity`.
- **Когда**: он определяет правила классификации diff и отказа на невалидном UTF-8.
- **Тогда**: документация `_changed_lines` явно описывает состояние открытого ханка.
- **И**: документация `scan_package_purity` описывает fail-closed `SyntaxError`, путь проблемного файла и сохранённый `UnicodeDecodeError` в `__cause__`.
- **checked_by**: `status: planned` `kind: manual` `owner: qa` `target: tests/runtime/test_core_purity.py::test_scan_package_purity_documents_invalid_utf8_contract`

## Матрица трассируемости

| Сценарий | Требования |
|---|---|
| BEH-01 | FR-01, FR-02, FR-06 |
| BEH-02 | FR-02, FR-07 |
| BEH-03 | FR-02, FR-07 |
| BEH-04 | FR-03 |
| BEH-05 | FR-04, FR-05 |
| BEH-06 | FR-04, FR-05 |
| BEH-07 | FR-05 |
| BEH-08 | FR-08, FR-09 |
| BEH-09 | FR-08 |
| BEH-10 | FR-01, FR-02, FR-05 |
| BEH-11 | FR-10, FR-11 |
| BEH-12 | FR-10, FR-12 |
| BEH-13 | FR-13 |
| BEH-14 | FR-08, FR-13 |
| BEH-15 | FR-14 |

Все FR-01–FR-14 покрыты как минимум одним сценарием BEH-01–BEH-15.
