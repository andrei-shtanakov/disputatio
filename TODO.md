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
- [ ] **D7-A — спека TDD lifecycle** для spec-runner @id:d7a-tdd-lifecycle-spec
  (`execution_mode: tdd`, фазовый FSM) как inbox-issue в spec-runner; артефакты
  D4 (`spec/.tdd-evidence/`, `scripts/tdd_gate.py`) — исходный материал.
  Блокер D5 снят PR #20.
- [ ] **D7-B — спека preflight/bootstrap** @id:d7b-preflight-bootstrap-spec
  (`--check/--plan/--apply`, presets) по транскриптам D0; friction-копилка —
  шаблоны spec-generator-skill (23 ruff-ошибки в greenfield), хрупкий
  литеральный парсинг вывода pytest, stale-кэш байткода из WARN 1 транскрипта
  D5 («git чист» ≠ «окружение чисто»). Блокер D5 снят PR #20.

## Бэклог

- [ ] `tdd_gate red --supersede` — v2 гейта @id:tdd-gate-red-supersede
  (осознанная замена red-эталона вместо ручного вмешательства оператора).
- [ ] Протокол D0 → редакция 4 @id:d0-protocol-rev4-mut01-flakiness
  — закрыть латентную флейкость D0-MUT-01 (WARN 1 транскрипта D5, PR #20):
  тик mtime-секунды между шагами 2 и 4 либо инвалидация `__pycache__` целевого
  теста в шаге 4; расширить шаг 5 проверкой «зелёный доказуем без сброса кэша»
  («git чист» ≠ «окружение чисто»). Материал уходит и в friction-копилку D7-B.

## Ждём от других проектов

- [ ] maestro: `validate --strict` не эскалирует warnings @id:maestro-strict-warnings-finding
  — вопреки README; находка D2, заведена как maestro#163; за владельцем maestro.
