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
  Отгружено срезами 0–4a: типизированные исходы фаз + `phase_results` (#167),
  `execution_mode: standard|tdd` (#171), RED-чекпойнт с replay в одноразовом
  worktree (#172), RED-гейт (#173), claims/byte-lock (#181), операторские
  remedies `abandon`/`repair` (#183), жизненный цикл как записанная FSM
  `tdd_phases` (#188). Issue закрыт 2026-08-14 как функционально выполненный.
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
  Read-only `preflight [--json]` отгружен (PR #158): восемь проверок со
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

- [ ] Оценка миграции TDD-гейта на штатный режим @id:tdd-gate-migration-assessment
  — `scripts/tdd_gate.py` (~2000 строк) и плагин `spec/plugins/tdd-gate`
  писались потому, что у spec-runner не было фаз: задаче негде было хранить
  «тест написан и подтверждённо падает». После отгрузки D7-A (срезы 0–4a)
  `execution_mode: tdd` — штатный контракт, и наш гейт его дублирует.
  Оценить: что покрывается штатно (RED-гейт, claims, `abandon`/`repair`/
  `resume`/`release`, `tdd_phases`), что теряется (независимый replay red-SHA,
  evidence в `spec/.tdd-evidence/` с неймспейсом по workstream, audit-фоллбэк
  `post_done` из `plugin.yaml`), и во что обходится переход. Это оценка, не
  миграция: решение принимается по её итогам.
- [ ] `tdd_gate red --supersede` — v2 гейта @id:tdd-gate-red-supersede
  (осознанная замена red-эталона вместо ручного вмешательства оператора).
  **Кандидат на снятие** по итогам
  `todo://disputatio/tdd-gate-migration-assessment`: штатные `tdd resume` /
  `repair` / `release` закрывают, судя по всему, тот же сценарий. Пока не
  снимаем — сперва оценка.
- [ ] Протокол D0 → редакция 4 @id:d0-protocol-rev4-mut01-flakiness
  — закрыть латентную флейкость D0-MUT-01 (WARN 1 транскрипта D5, PR #20):
  тик mtime-секунды между шагами 2 и 4 либо инвалидация `__pycache__` целевого
  теста в шаге 4; расширить шаг 5 проверкой «зелёный доказуем без сброса кэша»
  («git чист» ≠ «окружение чисто»). Материал уходит и в friction-копилку D7-B.

## Ждём от других проектов

- [ ] maestro: `validate --strict` не эскалирует warnings @id:maestro-strict-warnings-finding
  — вопреки README; находка D2, заведена как maestro#163; за владельцем maestro.
