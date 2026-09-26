# Прогон-доказательство: операторский remedy-путь под maestro (2026-09-26)

Закрывает пункт TODO `operator-remedy-path-under-maestro`. Прогон 2
(2026-08-22) заблокировался на залоченном RED-тесте с неверным ассертом, агент
назвал нужный remedy (`spec-runner tdd repair`), но автоповтор maestro стёр
состояние раньше, чем оператор успел его применить. Сосед закрыл это PR
maestro#212 (`bdc2a83`, «a deliberate TASK_BLOCKED refusal stops the automatic
retry»). Здесь путь оператора пройден целиком.

## Окружение

- maestro — `uv tool` из master соседа, коммит `e659eac` (2026-09-21),
  содержит мерж #212; прежняя установленная сборка `6d93ca3` (2026-08-20) его
  не содержала. `maestro --version` у обеих — 0.4.0: номер версии сосед не
  поднимал.
- spec-runner 4.1.0.
- **Изоляция.** Одноразовый клон disputatio (`3c02003`) в scratch-каталоге,
  `origin` удалён (пуш и PR физически невозможны), в клонированном
  `project.yaml`: `repo_path`/`workspace_base` — внутри scratch,
  `auto_pr: false`, `w-proof` заменён синтетическим `w-remedy`. После прогона
  сверено: наши `master` и `origin/master` — `3c02003`, веток `ws/*` и новых
  PR нет, настоящий `disputatio-ws/` не тронут.

## Сценарий

Синтетический workstream `w-remedy`: модуль `repo_probe.toplevel_exit_code`,
возвращающий фактический код `git rev-parse --show-toplevel`. В контракт
описания **намеренно** заложен неверный «измеренный» факт
`NOT_A_REPO_EXIT_CODE = 1`; фактический код вне репозитория — 128 (замер
перед прогоном). RED-тест обязан проверить и константу, и равенство ей кода
зонда, так что честная реализация его удовлетворить не может.

**Отличие от прогона 2.** Декомпозиция maestro сама измерила 128 и прямо
записала в задачу: при расхождении `128 == 1` остановиться с `TASK_BLOCKED` и
звать `tdd repair`. Блокировка здесь предписана планом, а не найдена агентом.
Проверяемый путь — поведение maestro и spec-runner после блокировки и
действия оператора — от этого не меняется.

## Ход

1. `maestro orchestrate project.yaml --db … --log-dir …` (08:41–08:45 UTC).
   RED: тест `tests/test_task_002_514b496c7388dba6_a1ece724_red.py::test_toplevel_exit_code_outside_repo`,
   переигран, красный; GREEN: «The contract can't be met without breaking
   another requirement» → `Fatal error (TASK_BLOCKED) -- no retry`.
   maestro: `workstream.retry.skipped workstream=w-remedy blocked=blocked`,
   статус `NEEDS_REVIEW`. **События workstream'а:** `decomposing → ready →
   running → needs_review` — ни `retrying`, ни повторной декомпозиции.
2. Состояние цело: worktree на месте, чекпоинт `ca96adb9bb89`
   (`expected_fail`, red-коммит `4478ee2`), тест залочен, черновик реализации
   в дереве не закоммичен, `attempts.error_code = TASK_BLOCKED`.
3. Оператор: исправил контракт в `spec/maestro-tasks.md` (1 → 128) и ассерт
   теста; закоммитил **только тест** (`d1022fa`) — без реализации, иначе
   переигрывание дало бы зелёный и `repair` отказал бы `NOT_RED`.
4. `spec-runner tdd repair TASK-002 --commit d1022fa --spec-prefix maestro-
   --reason "…"` из worktree (без `SPEC_RUNNER_AGENT`): «Red re-confirmed on
   the repaired commit», новая линия `7134408cfa9f` вместо `ca96adb9bb89`,
   запись в `tdd_remedies`. Код возврата команды в этом прогоне не
   зафиксирован (вывод шёл через `tail`); успех подтверждён сообщением
   `✔️ Repaired` и новой записью в БД.
5. `maestro workstream-continue w-remedy --db …` — `NEEDS_REVIEW → READY`
   поверх существующего tasks.md, без перегенерации.
6. `maestro orchestrate … --resume` (08:46–08:53): задача возобновлена с GREEN
   под новым чекпоинтом, code review пройден, `✅ Completed in 218.1s`,
   фаза `done`.
7. Evidence `spec/evidence/ws-w-remedy/TASK-002.json` несёт
   `remedies: [{operation: "repair", actor, reason, timestamp}]`.

Стоимость по state DB: попытки $0.33, вызовы агентов $0.86 (декомпозиция
maestro в эту БД не пишется).

## Итог

Remedy-путь под maestro **работает**: блокировка останавливает автоповтор,
состояние сохраняется, `tdd repair` принимается, `workstream-continue` +
`--resume` доводят задачу до `DONE`, evidence несёт запись о ремонте.

Workstream при этом закончился не `DONE`, а `FAILED → NEEDS_REVIEW` на
scope-гейте maestro (5 путей вне scope). Причины две, к remedy-пути не
относятся:

1. `logs/…/spec-runner-*.jsonl` — **артефакт постановки**: в настоящем репо
   `logs/` исключён через `.git/info/exclude`, а `git clone` этот файл не
   копирует.
2. RED-тест в `tests/test_task_002_<ns>_red.py` вне scope `tests/verifier/**` —
   **настоящая находка**: с сентября (spec-runner TASK-009 / #366) промпт
   RED-паса предписывает путь `tests/test_<task>_<ns>_red.py` в корне
   `tests/` (`spec_runner/tdd_runners.py::evidential_file`), и усыновление
   сверяет путь с ним. Scope наших workstream'ов, снявший страховочный глоб
   `tests/test_*.py` по опыту прогонов 2026-08-22, эту ситуацию больше не
   покрывает — следующая волна под maestro упрётся в scope escape так же.
   Отдельный пункт TODO.
