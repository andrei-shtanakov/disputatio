# TODO — disputatio (создан 2026-08-17, при вводе во флот)

> Роль в экосистеме: **ступень боевой обкатки №2** контура spec-runner/Maestro/steward
> (оси H1 декомпозиция, H2 параллелизм, H3 TDD-enforcement, H4 steward-diff) и
> одновременно продукт — headless-оркестратор author↔reviewer debate loop по
> SPEC-001 (`disputatio-SPEC-001-round-protocol.md` — авторитетный дизайн).
>
> Пункты уровня репо живут здесь; дизайн ступени D0–D7 — в зонтичном
> `_cowork_output/plans/2026-08-08-disputatio-battle-stage.md` (dev-only),
> протоколы и транскрипты сертификации — `docs/plans/`.
>
> Пункты могут быть размечены тегами на строке чекбокса:
> `@owner:<principal>` / `@blocked_by:<reference>` / `@trigger:"…"` / `@id:<node-id>`
> (грамматика `[a-z0-9][a-z0-9._-]{0,63}`, URI `todo://disputatio/<id>`).
> Отсутствие тега значит «неизвестно» — значения не выдумываем.

## Текущее состояние (2026-08-17, master `65df013`)

- ✅ **D0–D4 закрыты** (PR #1–#16): baseline-сертификация, D1-инварианты,
  TDD-gate (claim + независимый replay + операторские remedy abandon/repair,
  PR #15), волна 1 из 7 workstream'ов через `maestro orchestrate`
  (PR #11 → master, интеграционная ветка удалена — роль интеграционного
  состояния играет master).
- ✅ **MUT-02/03 закрыты** (PR #16): транскрипт
  `docs/plans/D0-transcript-integration-2026-08-10.md`, вердикт PASS
  (+ внеплановый MUT-04 на склейку цепочки).
- ✅ **D6 выполнен 2026-08-10** (вне критического пути): steward
  compile-and-diff против инвариантов D1, вердикт **semantic equivalence —
  PASS** (отчёт — зонтичный `_cowork_output/d6-steward-diff/REPORT.md`).
- ✅ Переезд под зонтик `all_ai_orchestrators` + ввод во флот — этот PR.

## Правила ведения

- Выполненный пункт → `[x]` + хеш коммита/номер PR.
- Ссылка на чужой репо — только с префиксом (`spec-runner#141`, `maestro#163`):
  голый `#N` GitHub резолвит в disputatio и уводит читателя не туда. Свои
  PR/issue пишем как есть.
- Прямые коммиты в `master` запрещены: ветка → PR → ревью Copilot → мержит человек.
- Чужие репо не правим: нужна правка у соседа — handoff в
  `../prograph-vault/authored/notes/`.

## Активные задачи

- [x] **D5 — полная интеграционная сертификация** @id:d5-integration-certification
  — по протоколу D0 с `phase=integration`: полный прогон таблицы проверок
  (GIT/ENV/TEST/LINT/TYPE/SPEC + MUT-01) как интеграционный транскрипт.
  Существующий integration-транскрипт (2026-08-10) покрывает только
  MUT-пробы. Нюанс из его WARN #3: интеграционной ветки нет — сертифицируется
  master. Выполнено PR #20: транскрипт
  `docs/plans/D0-transcript-integration-2026-08-17.md`, вердикт PASS, 2 WARN
  (WARN 1 — латентная флейкость D0-MUT-01, см. бэклог-пункт про редакцию 4).
- [x] **Теги `@id:` — на строки чекбоксов** @id:invisible-ids-on-continuation-lines
  — inbox-запрос devtools (#21, детектор DT-TAG-ON-CONTINUATION): построчные
  парсеры читают теги только со строки чекбокса, пункты с тегом на
  строке-продолжении жили без identity. Перенесены все шесть id-тегов файла
  (3 из issue + `d7a`/`d7b`/`tdd-gate-red-supersede` того же класса, детектором
  не пойманные — строка-продолжение с прозой перед тегом).
- [x] **D7-A — спека TDD lifecycle** для spec-runner @id:d7a-tdd-lifecycle-spec
  (`execution_mode: tdd`, фазовый FSM; исходный материал — артефакты D4
  `spec/.tdd-evidence/`, `scripts/tdd_gate.py`) — подана **2026-08-10** как
  inbox-issue spec-runner#141, ещё до снятия мнимого блокера D5. Принята
  владельцем как **design track**, а не minor-релиз; дизайн —
  `spec-runner/docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md`.
  Отгружено срезами 0–4a: типизированные исходы фаз + `phase_results`
  (spec-runner#167), `execution_mode: standard|tdd` (spec-runner#171),
  RED-чекпойнт с replay в одноразовом worktree (spec-runner#172), RED-гейт
  (spec-runner#173), claims/byte-lock (spec-runner#181), операторские remedies
  `abandon`/`repair` (spec-runner#183), жизненный цикл как записанная FSM
  `tdd_phases` (spec-runner#188). Issue закрыт 2026-08-14 как функционально выполненный.
  Две наши формулировки поправлены при приёме: «`standard` остаётся
  byte-identical» отвергнуто как невыполнимое по построению (срез 0 добавляет
  append-only строки) — действует «execution, terminal state и внешние
  контракты не меняются»; `WAIVED` не стал пятым вердиктом — waiver это
  решение оператора со своей причиной и provenance (`phase_waivers`), а не
  исход фазы. Остаток — automatic REFACTORING — вынесен в spec-runner#285
  (deferred by evidence trigger, счётчик 0/3). **На disputatio его не берём:**
  нашего `@blocked_by` на него нет, а владение чужим счётчиком без отдельного
  решения размыло бы границы ответственности.
- [x] **D7-B — спека preflight/bootstrap** @id:d7b-preflight-bootstrap-spec
  (`--check/--plan/--apply`, presets) по транскриптам D0 — подана **2026-08-10**
  как inbox-issue spec-runner#142, закрыта 2026-08-11 с разделением надвое.
  Read-only `preflight [--json]` отгружен (spec-runner#158): восемь проверок со
  статусами `ok·missing·empty·broken·unavailable·skipped` и отдельным флагом
  `blocking`; главное требование пилота выполнено буквально — `0 tests` не
  считается доказательством исправности (пустой набор — блокер со своим
  статусом). `bootstrap --check|--plan|--apply` и mutation probe вынесены в
  spec-runner#159 и **отклонены** решением владельца 2026-08-11
  (`bootstrap-product-boundary`): scaffolding — новая роль, а не естественное
  продолжение executor'а. Friction-копилка (шаблоны spec-generator-skill —
  23 ruff-ошибки в greenfield, хрупкий литеральный парсинг вывода pytest,
  stale-кэш байткода из WARN 1 транскрипта D5, «git чист» ≠ «окружение чисто»)
  не пропадает: она — материал бэклог-пункта `todo://disputatio/d0-protocol-rev4-mut01-flakiness`.

## Бэклог

- [x] Оценка миграции TDD-гейта на штатный режим @id:tdd-gate-migration-assessment
  — выполнена, отчёт `docs/plans/2026-08-21-tdd-gate-migration-assessment.md`
  (сверено против spec-runner 2.34.0, установленной версии). Итог: штатный
  контракт покрывает **все** механизмы нашего гейта и по четырём позициям
  строго сильнее — точка отказа до реализации (закрывает наш же blocking
  finding D4: RED-гейт отказывает на переходе в `green_implementing`, а не на
  стадии `tests`), учёт денег (`agent_calls`, NULL ≠ 0), пиновка политики
  (`config_hash` по `POLICY_KEYS`), иммунитет харнеса к агенту. Переход не
  требует правок у соседей и обновления версий: `execution_mode: tdd` кладётся
  в `extra_executor_config` (deep-merge), spec-runner читает legacy-обёртку
  `executor:`. Единственная содержательная потеря — класс артефакта: evidence
  не уничтожается и переживает снос worktree (post-mortem архив
  [maestro#164](https://github.com/andrei-shtanakov/maestro/issues/164) пишется вне worktree и невыключаем), но
  перестаёт быть трекаемым содержимым репо: единственная копия в
  операторском `db_dir` с ротацией `keep_per_workstream: 5` — не мержится,
  не видна в ревью, не реплицируется клонами. Рекомендация —
  гибрид: принять штатный гейт, заменив 1997 строк не нулём, а экспортёром
  evidence в трекаемые файлы.
- [x] Решение по миграции TDD-гейта @id:tdd-gate-migration-decision
  — **принято 2026-08-21: гибрид принимается.** Источник экспорта — живая
  `.executor-state.db` в worktree, пишем нормализованный трекаемый JSON до
  завершения задачи, чтобы evidence попадала в тот же commit/PR; post-mortem
  архив остаётся резервным источником для диагностики и восстановления, но не
  основным путём экспорта. Границы: включить штатный `execution_mode: tdd`;
  старую evidence не конвертировать; определить собственную стабильную схему
  экспорта, не выставляя наружу экспериментальные SQLite-таблицы; закрепить и
  проверять совместимую версию spec-runner; прогон-доказательство с полной
  цепочкой; до него ничего не удалять.
- [x] Экспортёр evidence — ядро @id:tdd-evidence-exporter-core
  — `scripts/tdd_evidence_export.py` (stdlib) + плагин
  `spec/plugins/tdd-evidence/plugin.yaml` на точке `post_review`,
  `blocking: true`; 16 тестов в `tests/evidence/`. Читает живую
  `.executor-*state.db`, пишет `spec/evidence/<ns>/<TASK>.json` по собственной
  схеме `disputatio/tdd-evidence/v1`. Полнота = данные, доступные ПЕРЕД DONE:
  подтверждённый red, claims, фазы включая `refactoring`, вердикт ревью и
  вердикты `tdd.red`/`tdd.claims`/`review`; сам DONE не требуется. Неполные
  данные не материализуются: недостающее печатается поимённо в stderr, код
  возврата ненулевой, прежний корректный артефакт не трогается — коммитится
  только `complete: true`. Запись каноническая (сортированные ключи, без
  времени экспорта) и атомарная, повтор даёт байт-в-байт тот же файл. Версия
  spec-runner пишется в артефакт и проверяется fail-closed (`MIN_SPEC_RUNNER`),
  как и наличие нужных таблиц/колонок.
- [x] Вердикт ревью в трекаемой evidence @id:tdd-evidence-review-half
  — закрыт тем же экспортёром: точка `post_review` отгружена соседом
  (spec-runner#307 → PR spec-runner#308, влит в их master), срабатывает после
  вердикта ревью и успешных пре-терминальных гейтов, до DONE-флипа и
  `commit_task_work`; записанное подметает `stage_all_except_runtime`.
  Оговорка соседа принята: под `wants_candidate` evidence ложится в финальный
  bookkeeping-коммит, а не в candidate — требование звучит как «в одной
  доставляемой истории/PR», а не «в том же коммите, что код». Наш вариант B
  (TDD-события в `audit_log`) отклонён владельцем с проверкой, которой у нас
  не было: `spec/.gitignore` игнорирует `.executor-*`, так что тот путь вообще
  не трекается.
- [x] Cutover на штатный режим @id:tdd-standard-mode-cutover
  — блокер снят: spec-runner выпустил **v2.35.0** (2026-08-21) с точкой
  `post_review`, установленная версия та же, `MIN_SPEC_RUNNER = (2, 35, 0)`
  экспортёра угадан верно и правки не потребовал. Сделано: `project.yaml` —
  `execution_mode: tdd` + `tdd_runner: pytest`, `test_command` очищен до
  `uv run pytest -q`, `harness_files` пересобран (гейт соседа править нечем,
  защищаем экспортёр, `spec/plugins`, `spec/evidence`, конституцию; старый гейт
  остаётся в списке, пока лежит в дереве); конституция переписана под штатный
  режим; операторские `spec-runner.config.example.yaml` и
  `docs/workstream-setup.md` приведены в соответствие.
  Три факта, проверенные по коду соседа и поменявшие план:
  **(1)** конституцию получают только пас реализации и ревьюер — RED-пас её
  промпта не видит (`prompt.build_red_prompt` собирается кодом), поэтому
  инструкции про `TDD_SELECTOR:` в неё не пишем: они уже встроены в spec-runner,
  а написанное туда правило просто не было бы доставлено;
  **(2)** композитный `test_command` в TDD-режиме отвергается целиком, а не
  теряет одно звено — вместе с `tdd_gate.py verify` из цепочки выпадает и место
  для `pyrefly check` (см. новый пункт бэклога);
  **(3)** полный suite сохраняется: narrowing включается только в
  parallel-режиме spec-runner, а maestro зовёт `spec-runner run --all`.
- [ ] Прогон-доказательство миграции @id:tdd-gate-migration-proof
  — одна leaf-задача с полной цепочкой RED → GREEN → review → трекаемая
  evidence. Cutover выполнен (`todo://disputatio/tdd-standard-mode-cutover`),
  релиз опубликован — ждать больше нечего. **Уровень прогона: полный
  `maestro orchestrate`** (решение 2026-08-22) — доказывается не работа
  leaf-задачи, а весь production-путь после cutover'а: maestro → worktree →
  ветка `ws/<id>` → spec-runner → RED → GREEN → review → pyrefly → трекаемая
  evidence → PR. Прямой запуск spec-runner обошёл бы ровно те интеграционные
  границы, которые и проверяются. Доказательство засчитывается только после
  проверки содержимого PR, включая `spec/evidence/ws-<id>/<TASK>.json`.
  Подготовка (этот PR): `base_branch` → `master` (`pilot/wave-1` удалена после
  PR #11, а worktree создаётся именно от base — `validate --strict` этого не
  ловит, файловый ярус ветку не проверяет); состав workstream'ов заменён одним
  `w-proof` (у `orchestrate` нет фильтра по workstream'у — он берёт весь
  список); задача взята из настоящего долга репо — TODO в
  `src/disputatio/verifier/diffstats.py`, где любой ненулевой код возврата git
  читается как «изменений нет».
  Развилка про red-файл закрыта **обеими** страховками: описание задачи ведёт
  RED-пас в `tests/verifier/`, а scope-глоб `tests/test_*.py` страхует от
  встроенного промпта, который предлагает корень `tests/`. Глоб именно
  `test_*.py`, а не `test_*_red.py`: второй не матчит ни одного файла и роняет
  `validate --strict` через `scope-no-match`, а заглушку создать нельзя — red
  в предсуществующем файле отвергается. Помечен как временное исключение
  w-proof; после прогона — issue соседу на системный фикс (RED-путь
  настраиваемый или scope-aware), затем глоб снять.
  Третья находка подготовки: `spec/evidence/**` обязан быть в scope —
  orchestrator-managed для scope-гейта только `spec/maestro-*`,
  `spec/.maestro-*` и `spec/.executor-*` (код соседа — maestro,
  `maestro/changed_paths.py`, не файл этого репо), так что
  артефакт экспортёра иначе прочитался бы как scope escape.
  Только после прогона удаляются `scripts/tdd_gate.py` (1997 строк),
  `tests/harness/` (2725 строк, 130 тестов), `spec/plugins/tdd-gate/`, и
  закрывается `todo://disputatio/tdd-gate-red-supersede` штатными
  `resume`/`repair`/`release`.
- [x] Имя каталога трекаемой evidence @id:tdd-evidence-namespace-dir
  — экспортёр кладёт артефакт в `spec/evidence/<namespace>/<TASK>.json`, беря
  namespace из БД. `tdd_namespace` в `project.yaml` объявить нельзя: ключ один
  на весь конфиг, а неймспейс обязан быть свой у каждого workstream'а (INV-16),
  и per-workstream оверлея у maestro нет (`WorkstreamConfig` такого поля не
  имеет). Пустое значение даёт `sha256(путь worktree + spec_prefix)[:16]`:
  изоляция верная — у каждого worktree своя БД и свой хеш, коллизий при
  схождении веток нет, — но каталог получается нечитаемым и меняется при смене
  пути worktree, то есть evidence одного workstream'а может разъехаться по
  двум каталогам. Это ровно то свойство (evidence как читаемый продукт репо),
  ради которого выбран гибрид. **Решено: экспортёр выводит имя сам** —
  `resolve_export_namespace` берёт `ws/<id>` → `ws-<id>` в worktree Maestro и
  кладёт его И в каталог, И в поле `namespace`; сырой неймспейс БД сохранён
  рядом как `state_namespace` (по нему находятся строки в живой БД и в
  post-mortem архиве). Хеш остался fallback'ом только для прогонов вне
  workstream'а. Неожиданная ветка в maestro-дереве — отказ, а не тихий
  fallback (INV-18); форма строго `ws/<id>` без внутренних `/`, иначе `ws/a/b`
  и `ws/a-b` поделили бы один каталог. 5 тестов в `tests/evidence/`.
- [x] Место для pyrefly в пер-тасковом гейте @id:pyrefly-in-standard-gate
  — до cutover'а typecheck в `project.yaml` не стоял (он жил только в
  рукописном операторском конфиге — дрейф старше миграции), а теперь и не может
  стоять там, где стоял: композитный `test_command` в TDD-режиме отвергается, а
  композитный `lint_command` гоняется RED-фазой целиком перед заморозкой файла
  (`_lint_claimed` в spec-runner), где реализации ещё нет — проектный pyrefly
  там красный по
  построению и отказывал бы каждый red. Оплаченный урок пилота (типовой долг
  на 22 задачи, вскрытый на byte-locked тестах) при этом в силе.
  **Решено: блокирующий плагин `spec/plugins/pyrefly/` на `post_review`.**
  Порядок хуков проверен на `discover_plugins` (spec-runner): они идут по
  алфавиту **имён
  плагинов**, поэтому `pyrefly` исполняется перед `tdd-evidence` — цепочка
  RED → GREEN → review → pyrefly → evidence, и ошибка типизации останавливает
  задачу до фиксации evidence. Версия зафиксирована `uv.lock` (pyrefly 1.2.0).
- [ ] `tdd_gate red --supersede` — v2 гейта @id:tdd-gate-red-supersede
  (осознанная замена red-эталона вместо ручного вмешательства оператора).
  **Кандидат на снятие**: оценка показала (§4.4 отчёта), что сценарий закрыт
  штатными `tdd resume` / `repair` / `release`. Снимаем не по тексту оценки, а
  по прогону-доказательству из `todo://disputatio/tdd-gate-migration-decision`.
- [x] Протокол D0 → редакция 4 @id:d0-protocol-rev4-mut01-flakiness
  — WARN 1 транскрипта D5 закрыт: `docs/plans/D0-certification-protocol.md`,
  редакция 4. D0-MUT-01 стал семишаговым — добавлен шаг 4 «тик mtime-секунды»
  между мутацией и восстановлением (мутация `0.1.0` → `9.9.9` не меняет размер
  файла, а заголовок `.pyc` хранит mtime с точностью до секунды, поэтому
  одна секунда на оба действия делала stale-кэш валидным), а шаг 7 требует
  зелёного **без сброса кэша**. Красный шаг 7 больше не «провал пробы наугад»:
  диагностика D-1/D-2 (удалить только кэш целевого теста и повторить)
  различает грязное окружение — категория `ERROR`, проба недействительна и
  перепрогоняется, обе попытки в транскрипте — и несработавшее восстановление,
  то есть настоящий провал. Обе ветки исполнены на живом репо перед фиксацией.
  Общий урок вынесен в принцип раздела «Проверки»: «git чист» ≠ «окружение
  чисто». Из выбора WARN 1 взят тик, а не инвалидация кэша в шаге
  восстановления: второе сделало бы проверку шага 7 бессмысленной — она
  доказывала бы чистоту окружения, которое сама же и почистила.

## Ждём от других проектов

- [x] maestro: `validate --strict` не эскалирует warnings @id:maestro-strict-warnings-finding
  — находка D2, заведена как maestro#163. **Вердикт владельца maestro: не
  воспроизводится**; issue закрыт 2026-08-10, опровержение — коммит `cd30f00`
  (maestro#168): и на HEAD, и на том самом установленном 0.4.x
  `validate --strict`
  даёт exit 1, а строка эскалации `if not report.ok or (strict and
  report.warnings)` не менялась с `7277700` (2026-07-04). Механизм исходного
  наблюдения — pipe-masked exit code (`… | tail -1`); владелец заявил это как
  гипотезу, а не факт: инвокация в нашем отчёте записана не была. Тот же класс
  у нас уже закрыт правилом протокола D0 («exit code берётся у проверяемой
  команды, не у последнего элемента пайпа», `D0-certification-protocol.md`).
  Побочный результат разбора: контракт закреплён тестами и уточнён в README
  maestro — `--strict --no-fs` проверяет строго **меньший набор условий**, чем
  `--strict`, а не тот же набор мягче. Отсюда правка комментария в
  `project.yaml` этим же PR.
  Ожидание простояло протухшим 11 дней, потому что было машинно невидимым: у
  пункта не было тега `@blocked_by` с URI `todo://maestro/163` — он жил под
  прозаическим заголовком секции (правило — `cross-repo-waits.md`).
