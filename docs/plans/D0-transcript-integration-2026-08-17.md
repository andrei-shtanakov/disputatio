---
title: D0-транскрипт — полная таблица проверок + D0-MUT-01, фаза integration (D5)
date: 2026-08-17
phase: integration
base_sha: e98bcc13cce18c908fa7129b1712111feab6c11f
branch: cert/d5-integration (от master; интеграционной ветки нет — см. WARN 2)
revision: 1
verdict: PASS
---

# D5 — полная интеграционная сертификация по протоколу D0

Полный прогон таблицы проверок протокола D0 (редакция 3) с `phase=integration`:
GIT/ENV/TEST/LINT/TYPE/SPEC + D0-MUT-01. Существующий интеграционный транскрипт
(`D0-transcript-integration-2026-08-10.md`) покрывал только MUT-пробы
(MUT-02/03/04); этот закрывает остальную таблицу и тем самым пункт D5.

Сертифицируется состояние `master` на `e98bcc1` (после мержа PR #19 —
нормализация CLAUDE.md; кодовая база не менялась с интеграции волны 1).

Главная находка прогона: **первый прогон D0-MUT-01 дал ложный красный на
шаге 6** — не из-за оракула, а из-за stale-кэша байткода pytest. Root cause
доказан, проба перепрогнана начисто, детали — в разделах про MUT-01 и WARN 1.

## Что сделано с сырым выводом (объявляется, а не умалчивается)

1. **stdout и stderr сохранены раздельно**, каждый своим блоком; пустые блоки
   опущены с пометкой в тексте.
2. **Абсолютный путь окружения заменён на `<repo>`** — единственная редактура
   текста вывода.
3. Обрезки нет: все блоки приведены целиком.
4. Все команды исполнялись через `subprocess.run(capture_output=True)` без
   пайпов; exit-код фиксировался у самой проверяемой команды (принцип D0).

## Сводная таблица

| check_id | команда как выполнена | exit | категория | заметка |
|---|---|---|---|---|
| D0-GIT-01 | `git rev-parse --is-inside-work-tree` | 0 | OK | `true` |
| D0-GIT-02 | `git rev-list --count HEAD` | 0 | OK | 471 коммит |
| D0-GIT-03 | `git branch --show-current` | 0 | OK | `cert/d5-integration` |
| D0-GIT-04 | `git remote get-url origin` | 0 | OK | `git@github.com:andrei-shtanakov/disputatio.git` |
| D0-GIT-05 | `git status --porcelain` | 0 | OK | вывод пуст — дерево чистое |
| D0-ENV-01 | `uv sync --dev` | 0 | OK | `Resolved 18 packages … Checked 17 packages` |
| D0-TEST-01 | `uv run pytest -q --collect-only` | 0 | OK | collection без ошибок |
| D0-TEST-02 | тот же вывод D0-TEST-01 (команда идентична, не перезапускалась) | 0 | OK | строка `1207 tests collected in 0.35s`, N ≥ 1 |
| D0-TEST-03 | `uv run pytest -q` | 0 | OK | `1203 passed, 4 skipped, 4 warnings` |
| D0-LINT-01 | `uv run ruff check .` | 0 | OK | `All checks passed!` |
| D0-TYPE-01 | `uv run pyrefly check` | 0 | OK | `0 errors` |
| D0-SPEC-01 | `test -s disputatio-SPEC-001-round-protocol.md` | 0 | OK | спека на месте, непустая |
| D0-MUT-01 (прогон 1) | 6 шагов по протоколу | — | **провал пробы** | шаг 6 exit 1 — ложный красный, см. root cause |
| D0-MUT-01 (прогон 2) | 6 шагов + тик mtime-секунды перед шагом 4 | — | **PASS** | шаг 3 EXPECTED_FAIL по нужной причине, шаг 6 OK |

Прогон 2 — канонический результат пробы; прогон 1 сохранён целиком как
доказательная база WARN 1.

## Табличные проверки — сырой вывод

### D0-TEST-03 — `uv run pytest -q`, exit 0

stdout:

```
........................................................................ [  5%]
........................................................................ [ 11%]
........................................................................ [ 17%]
........................................................................ [ 23%]
.......................ss............................................... [ 29%]
........................................................................ [ 35%]
........................................................................ [ 41%]
........................................................................ [ 47%]
........................................................................ [ 53%]
........................................................................ [ 59%]
.............................................ss......................... [ 65%]
........................................................................ [ 71%]
........................................................................ [ 77%]
........................................................................ [ 83%]
........................................................................ [ 89%]
........................................................................ [ 95%]
.......................................................                  [100%]
=============================== warnings summary ===============================
tests/contracts/test_enum_comparisons.py::test_string_verdict_approve_on_failed_gates_rejected
  <repo>/.venv/lib/python3.12/site-packages/pydantic/main.py:475: UserWarning: Pydantic serializer warnings:
    PydanticSerializationUnexpectedValue(Expected `enum` - serialized value may not be as expected [field_name='verdict', input_value='approve', input_type=str])
    return self.__pydantic_serializer__.to_python(

tests/contracts/test_enum_comparisons.py::test_string_verdict_request_changes_without_issues_rejected
  <repo>/.venv/lib/python3.12/site-packages/pydantic/main.py:475: UserWarning: Pydantic serializer warnings:
    PydanticSerializationUnexpectedValue(Expected `enum` - serialized value may not be as expected [field_name='verdict', input_value='request_changes', input_type=str])
    return self.__pydantic_serializer__.to_python(

tests/contracts/test_enum_comparisons.py::test_string_overall_fail_triggers_rule
  <repo>/.venv/lib/python3.12/site-packages/pydantic/main.py:475: UserWarning: Pydantic serializer warnings:
    PydanticSerializationUnexpectedValue(Expected `enum` - serialized value may not be as expected [field_name='overall', input_value='fail', input_type=str])
    return self.__pydantic_serializer__.to_python(

tests/contracts/test_enum_comparisons.py::test_invalid_verdict_string_raises_validation_error
  <repo>/.venv/lib/python3.12/site-packages/pydantic/main.py:475: UserWarning: Pydantic serializer warnings:
    PydanticSerializationUnexpectedValue(Expected `enum` - serialized value may not be as expected [field_name='verdict', input_value='yolo', input_type=str])
    return self.__pydantic_serializer__.to_python(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1203 passed, 4 skipped, 4 warnings in 82.69s (0:01:22)
```

stderr: (пусто)

### D0-TYPE-01 — `uv run pyrefly check`, exit 0

stdout: (пусто)

stderr:

```
 INFO Checking project configured at `<repo>/pyrefly.toml`
 INFO 0 errors (10 suppressed, 2 warnings not shown)
```

## D0-MUT-01 — прогон 1 (провал пробы: ложный красный шага 6)

Шаги 1–5 отработали по протоколу; провалился шаг 6.

### 1. зафиксировать базу

`git rev-parse HEAD` → `e98bcc13cce18c908fa7129b1712111feab6c11f`, exit 0;
`git status --porcelain` → пусто, exit 0 — OK.

### 2. сломать assertion

`python3 -c "import pathlib; p = pathlib.Path('tests/test_smoke.py'); p.write_text(p.read_text().replace('0.1.0', '9.9.9'))"` → exit 0;
доказательство мутации: `git diff --quiet tests/test_smoke.py` → **exit 1**
(файл изменён) — OK.

### 3. подтвердить красный

`uv run pytest -q tests/test_smoke.py::test_package_importable` → exit 1 —
**EXPECTED_FAIL** (в выводе `AssertionError`, падает именно выбранный тест).

stdout:

```
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_package_importable ____________________________

    def test_package_importable() -> None:
>       assert __version__ == "9.9.9"
E       AssertionError: assert '0.1.0' == '9.9.9'
E         
E         - 9.9.9
E         + 0.1.0

tests/test_smoke.py:7: AssertionError
=========================== short test summary info ============================
FAILED tests/test_smoke.py::test_package_importable - AssertionError: assert ...
1 failed in 0.01s
```

stderr: (пусто)

### 4. восстановить

`git checkout -- tests/test_smoke.py` → exit 0 — OK.

### 5. проверить восстановление

`git rev-parse HEAD` == SHA шага 1: **True**; `git status --porcelain` пуст;
содержимое `tests/test_smoke.py` байт-в-байт как до пробы: **True** — OK.

### 6. подтвердить зелёный — ПРОВАЛ

`uv run pytest -q tests/test_smoke.py::test_package_importable` → **exit 1**
вместо ожидаемого 0.

stdout:

```
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_package_importable ____________________________

    def test_package_importable() -> None:
>       assert __version__ == "0.1.0"
E       AssertionError: assert '0.1.0' == '9.9.9'
E         
E         - 9.9.9
E         + 0.1.0

tests/test_smoke.py:7: AssertionError
=========================== short test summary info ============================
FAILED tests/test_smoke.py::test_package_importable - AssertionError: assert ...
1 failed in 0.01s
```

stderr: (пусто)

Вывод противоречит сам себе: показанный исходник уже восстановлен
(`assert __version__ == "0.1.0"`), но исполненное сравнение — с `'9.9.9'`.
Исполнялся не тот код, что лежит на диске.

## Root cause прогона 1 — stale-кэш байткода (доказан)

Механизм: заголовок `.pyc` хранит mtime исходника **с точностью до секунды**
и его размер. Мутация `0.1.0` → `9.9.9` размер не меняет (5 байт → 5 байт —
это свойство самой пробы), а мутация (шаг 2) и восстановление (шаг 4)
уложились в одну mtime-секунду. Скомпилированный на шаге 3
assertion-rewrite-кэш pytest (`tests/__pycache__/test_smoke.cpython-312-pytest-9.1.1.pyc`)
после restore считался актуальным и продолжал исполнять литерал `9.9.9`.

Доказательная цепочка (все команды — на восстановленном дереве, git-чистом):

| шаг | факт | результат |
|---|---|---|
| RC-0 | stale `.pyc` содержит байты `9.9.9`, не содержит `0.1.0` | подтверждено чтением файла |
| RC-1 | `uv run pytest -q tests/test_smoke.py::test_package_importable` | exit 1 — красный на восстановленном исходнике |
| RC-2 | удалён только `tests/__pycache__/test_smoke.cpython-312-pytest-9.1.1.pyc`; никакие трекаемые файлы не менялись | — |
| RC-3 | та же команда pytest | exit 0, `1 passed` — зелёный вернулся |

RC-3, stdout:

```
.                                                                        [100%]
1 passed in 0.00s
```

Следствие для протокола: шаг 5 («восстановление побайтово + HEAD совпал»)
проверяет только git-состояние и **не ловит** грязное состояние окружения —
после прогона 1 репозиторий оставался в положении «git чист, а suite красный»
до сброса кэша.

## D0-MUT-01 — прогон 2 (канонический) — **PASS**

Шаги идентичны протоколу; единственное отклонение объявляется явно:
между шагом 3 и шагом 4 вставлен `sleep 1.2s`, чтобы mtime восстановленного
файла попал в другую секунду, чем у мутированного (устранение confound'а из
root cause; кандидат в редакцию 4 протокола — WARN 1).

### 1. зафиксировать базу

`git rev-parse HEAD` → `e98bcc13cce18c908fa7129b1712111feab6c11f`, exit 0;
`git status --porcelain` → пусто, exit 0 — OK.

### 2. сломать assertion

Та же команда мутации → exit 0; `git diff --quiet tests/test_smoke.py` →
**exit 1** (файл изменён) — OK.

### 3. подтвердить красный

`uv run pytest -q tests/test_smoke.py::test_package_importable` → exit 1 —
**EXPECTED_FAIL**.

stdout:

```
F                                                                        [100%]
=================================== FAILURES ===================================
___________________________ test_package_importable ____________________________

    def test_package_importable() -> None:
>       assert __version__ == "9.9.9"
E       AssertionError: assert '0.1.0' == '9.9.9'
E         
E         - 9.9.9
E         + 0.1.0

tests/test_smoke.py:7: AssertionError
=========================== short test summary info ============================
FAILED tests/test_smoke.py::test_package_importable - AssertionError: assert ...
1 failed in 0.01s
```

stderr: (пусто)

### 4. восстановить

(после `sleep 1.2s`) `git checkout -- tests/test_smoke.py` → exit 0 — OK.

### 5. проверить восстановление (blocking в phase=integration)

`git rev-parse HEAD` == SHA шага 1: **True**; `git status --porcelain` пуст;
содержимое байт-в-байт как до пробы: **True** — OK.

### 6. подтвердить зелёный

`uv run pytest -q tests/test_smoke.py::test_package_importable` → exit 0 — OK.

stdout:

```
.                                                                        [100%]
1 passed in 0.00s
```

stderr: (пусто)

## Вердикт фазы

**PASS.** Все blocking-проверки таблицы — OK; D0-MUT-01 в каноническом прогоне
даёт предписанный EXPECTED_FAIL на шаге 3 (красный по нужной причине — падает
именно выбранный тест с `AssertionError`) и OK на остальных шагах, включая
blocking-шаг 5 фазы integration. Ложный красный прогона 1 доказательно отнесён
к измерительному стенду (кэш байткода), а не к оракулу: зелёный возвращается
удалением одного файла кэша без единого изменения трекаемых файлов.

## WARN

1. **D0-MUT-01 в редакции 3 латентно флейки.** Мутация по построению сохраняет
   размер файла (`0.1.0` → `9.9.9`), поэтому при мутации и restore в одну
   mtime-секунду assertion-rewrite-кэш pytest не инвалидируется и шаг 6 даёт
   ложный красный (воспроизведено, root cause доказан). Хуже того: после
   такого прогона дерево git-чистое, а suite красный — состояние, которое
   шаг 5 не ловит. Кандидат в редакцию 4 протокола: тик mtime-секунды между
   шагами 2 и 4 (или инвалидация `__pycache__` целевого теста в шаге 4) +
   расширение шага 5 проверкой «зелёный доказуем без сброса кэша». Также это
   вход в friction-копилку D7-B (preflight/bootstrap): bootstrap обязан знать,
   что «git чист» ≠ «окружение чисто».
2. **Интеграционной ветки нет — сертифицируется `master`.** Наследие WARN 3
   транскрипта 2026-08-10: `pilot/wave-1` удалена по правилу полирепо, её роль
   перешла к `master`. Прогон выполнен на ветке `cert/d5-integration`,
   созданной от `master e98bcc1` без единого коммита сверху на момент прогона
   (состояние дерева идентично `master`); буква протокола «прогон на
   интеграционной ветке» выполнена той же подстановкой, что и в прошлый раз.
