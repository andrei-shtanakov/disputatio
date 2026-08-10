---
title: D0-транскрипт — MUT-02 / MUT-03 (+ MUT-04), фаза integration
date: 2026-08-10
phase: integration
base_sha: 86365817251327f78174d152985259ca740921d7
branch: cert/mut-02-03 (от master после мержа PR #11 и #15)
verdict: PASS
---

# MUT-02 / MUT-03 — mutation_probe линта и типов

Follow-up протокола D0 (редакция 3, раздел «Follow-up»): mutation_probe по
6-шаговой схеме D0-MUT-01, применённой к области линта и типов вместо теста.
По статусу протокола эти пробы «блокируют окончательную спецификацию D7-B и
должны быть закрыты до повторной интеграционной сертификации D5».

Зачем вообще: **зелёный прибор ничего не доказывает, пока не показано, что он
краснеет на заведомом дефекте.** Полный suite, `ruff check .` и
`pyrefly check` были зелёными весь пилот — но зелёными они бывают и когда
смотрят не туда. D0-TEST-02 именно так и прошёл когда-то мимо: `| tail -1`
брал exit-код у пайпа, и ветка ERROR была недостижима.

## Что проверялось

| Проба | Прибор | Внесённый дефект | Ожидаемый красный |
|---|---|---|---|
| MUT-02 | `ruff check .` | несортированные и неиспользуемые импорты в начале модуля | `I001` |
| MUT-03 | `pyrefly check` | `BROKEN: int = "не число"` | `bad-assignment` |
| MUT-04 | `pyrefly check && pytest -q` | тот же типовой дефект | цепочка падает на pyrefly, не доходя до pytest |

Мишень мутации — `src/disputatio/contracts/decision.py` (трекаемый файл, чтобы
восстановление шло `git checkout --`, а не пересозданием).

**MUT-02 представителен не случайно**: `I001` — ровно та ловушка, которая за
волну 1 сработала трижды и породила disputatio#7. Проба показывает, что сам
линтер её видит; проблема была не в приборе, а в том, что байт-лок
консервировал долг раньше, чем линтер до него добирался.

**MUT-04 в исходную схему не входил** и добавлен по находкам этой сессии:
раннер исполняет `test_command` одной shell-строкой, а spec-runner#139
показал, что строку могут переписать. Звено, краснеющее в одиночку, — ещё не
краснеющий гейт, и это стоит проверять отдельно.

## Соблюдение принципа D0 об exit-коде

Во всех трёх пробах exit-код берётся **у проверяемой команды**: вывод
захватывается через `capture_output`, никаких пайпов. Доказательство самой
мутации — `git diff --quiet` с exit 1, а не exit 0 команды правки: «правка
отработала» и «файл изменился» — разные утверждения, и первое без второго
ничего не значит.

## Восстановление проверяется побайтово

Шаг 5 сверяет не только `HEAD` и чистоту дерева, но и равенство содержимого
файла исходному байт-в-байт. Совпадения `git status` недостаточно: файл,
восстановленный «похоже», прошёл бы такую проверку.

## Сводная таблица

| check_id | команда как выполнена | exit | категория | заметка |
|---|---|---|---|---|
| MUT-02.1 | `git rev-parse HEAD; git status --porcelain` | 0 | OK | зафиксировать базу |
| MUT-02.2 | `prepend в src/disputatio/contracts/decision.py; git diff --quiet -- src/disputatio/contracts/decision.py` | 1 | OK | внести дефект |
| MUT-02.3 | `uv run ruff check .` | 1 | EXPECTED_FAIL | подтвердить красный |
| MUT-02.4 | `git checkout -- src/disputatio/contracts/decision.py` | 0 | OK | восстановить |
| MUT-02.5 | `git rev-parse HEAD; git status --porcelain; побайтовое сравнение` | 0 | OK | проверить восстановление |
| MUT-02.6 | `uv run ruff check .` | 0 | OK | подтвердить зелёный |
| MUT-03.1 | `git rev-parse HEAD; git status --porcelain` | 0 | OK | зафиксировать базу |
| MUT-03.2 | `prepend в src/disputatio/contracts/decision.py; git diff --quiet -- src/disputatio/contracts/decision.py` | 1 | OK | внести дефект |
| MUT-03.3 | `uv run pyrefly check` | 1 | EXPECTED_FAIL | подтвердить красный |
| MUT-03.4 | `git checkout -- src/disputatio/contracts/decision.py` | 0 | OK | восстановить |
| MUT-03.5 | `git rev-parse HEAD; git status --porcelain; побайтовое сравнение` | 0 | OK | проверить восстановление |
| MUT-03.6 | `uv run pyrefly check` | 0 | OK | подтвердить зелёный |
| MUT-04.1 | `git status --porcelain` | 0 | OK | база чиста |
| MUT-04.2 | `git diff --quiet -- src/disputatio/contracts/decision.py` | 1 | OK | внести типовой дефект |
| MUT-04.3 | `uv run pyrefly check && uv run pytest -q` | 1 | EXPECTED_FAIL | прогнать цепочку целиком |
| MUT-04.4 | `git checkout -- src/disputatio/contracts/decision.py` | 0 | OK | восстановить и сверить побайтово |
| MUT-04.5 | `uv run pyrefly check && uv run pytest -q` | 0 | OK | цепочка зелёная на восстановленном |

## MUT-02 (ruff) — **PASS**

### 1. зафиксировать базу

Команда: `git rev-parse HEAD; git status --porcelain`  
exit: `0` — OK

```
HEAD=86365817251327f78174d152985259ca740921d7
porcelain=''
```

### 2. внести дефект

Команда: `prepend в src/disputatio/contracts/decision.py; git diff --quiet -- src/disputatio/contracts/decision.py`  
exit: `1` — OK

```
git diff --quiet exit=1 (1 = файл изменён, мутация состоялась)
```

### 3. подтвердить красный

Команда: `uv run ruff check .`  
exit: `1` — EXPECTED_FAIL

```
I001 [*] Import block is un-sorted or un-formatted
 --> src/disputatio/contracts/decision.py:1:1
  |
1 | / import os
2 | | import collections
  | |__________________^
3 |   """Модель `decision.json` — Decision, Outcome ([DESIGN-006], [REQ-006]).
  |
help: Organize imports
  |
1 + import collections
2 | import os
  - import collections
3 +
4 | """Модель `decision.json` — Decision, Outcome ([DESIGN-006], [REQ-006]).
  |

F401 [*] `os` imported but unused
 --> src/disputatio/contracts/decision.py:1:8
  |
1 | import os
  |        ^^
2 | import collections
3 | """Модель `decision.json` — Decision, Outcome ([DESIGN-006], [REQ-006]).
  |
help: Remove unused import: `os`
  |
  - import os
1 | import collections
  |

F401 [*] `collections` imported but unused
 --> src/disputatio/contracts/decision.py:2:8
  |
1 | import os
2 | import collections
  |        ^^^^^^^^^^^
3 | """Модель `decision.json` — Decision, Outcome ([DESIGN-006], [REQ-006]).
  |
help: Remove unused import: `collections`
  |
1 | import os
  - import collections
2 | """Модель `decision.json` — Decision, Outcome ([DESIGN-006], [REQ-006]).
  |

Found 3 errors.
[*] 3 fixable with the `--fix` option.
```

### 4. восстановить

Команда: `git checkout -- src/disputatio/contracts/decision.py`  
exit: `0` — OK

```
(без вывода)
```

### 5. проверить восстановление

Команда: `git rev-parse HEAD; git status --porcelain; побайтовое сравнение`  
exit: `0` — OK

```
HEAD=86365817251327f78174d152985259ca740921d7 (== база: True)
porcelain=''
байт-в-байт как до пробы: True
```

### 6. подтвердить зелёный

Команда: `uv run ruff check .`  
exit: `0` — OK

```
All checks passed!
```

## MUT-03 (pyrefly) — **PASS**

### 1. зафиксировать базу

Команда: `git rev-parse HEAD; git status --porcelain`  
exit: `0` — OK

```
HEAD=86365817251327f78174d152985259ca740921d7
porcelain=''
```

### 2. внести дефект

Команда: `prepend в src/disputatio/contracts/decision.py; git diff --quiet -- src/disputatio/contracts/decision.py`  
exit: `1` — OK

```
git diff --quiet exit=1 (1 = файл изменён, мутация состоялась)
```

### 3. подтвердить красный

Команда: `uv run pyrefly check`  
exit: `1` — EXPECTED_FAIL

```
ERROR `Literal['не число']` is not assignable to `int` [bad-assignment]
 --> src/disputatio/contracts/decision.py:1:15
  |
1 | BROKEN: int = "не число"
  |         ---   ^^^^^^^^^^
  |         |
  |         declared type
  |
 INFO Checking project configured at `/Users/Andrei_Shtanakov/labs/disputatio/pyrefly.toml`
 INFO 1 error (10 suppressed, 2 warnings not shown)
```

### 4. восстановить

Команда: `git checkout -- src/disputatio/contracts/decision.py`  
exit: `0` — OK

```
(без вывода)
```

### 5. проверить восстановление

Команда: `git rev-parse HEAD; git status --porcelain; побайтовое сравнение`  
exit: `0` — OK

```
HEAD=86365817251327f78174d152985259ca740921d7 (== база: True)
porcelain=''
байт-в-байт как до пробы: True
```

### 6. подтвердить зелёный

Команда: `uv run pyrefly check`  
exit: `0` — OK

```
INFO Checking project configured at `/Users/Andrei_Shtanakov/labs/disputatio/pyrefly.toml`
 INFO 0 errors (10 suppressed, 2 warnings not shown)
```

## MUT-04 (цепочка гейта) — **PASS**

### 1. база чиста

Команда: `git status --porcelain`  
exit: `0` — OK

```
''
```

### 2. внести типовой дефект

Команда: `git diff --quiet -- src/disputatio/contracts/decision.py`  
exit: `1` — OK

```
1 = мутация состоялась
```

### 3. прогнать цепочку целиком

Команда: `uv run pyrefly check && uv run pytest -q`  
exit: `1` — EXPECTED_FAIL

```
ERROR `Literal['не число']` is not assignable to `int` [bad-assignment]
 --> src/disputatio/contracts/decision.py:1:15
  |
1 | BROKEN: int = "не число"
  |         ---   ^^^^^^^^^^
  |         |
  |         declared type
  |
 INFO Checking project configured at `/Users/Andrei_Shtanakov/labs/disputatio/pyrefly.toml`
 INFO 1 error (10 suppressed, 2 warnings not shown)
```

### 4. восстановить и сверить побайтово

Команда: `git checkout -- src/disputatio/contracts/decision.py`  
exit: `0` — OK

```
байт-в-байт: True
```

### 5. цепочка зелёная на восстановленном

Команда: `uv run pyrefly check && uv run pytest -q`  
exit: `0` — OK

```
t be as expected [field_name='verdict', input_value='yolo', input_type=str])
    return self.__pydantic_serializer__.to_python(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1203 passed, 4 skipped, 4 warnings in 76.24s (0:01:16)
 INFO Checking project configured at `/Users/Andrei_Shtanakov/labs/disputatio/pyrefly.toml`
 INFO 0 errors (10 suppressed, 2 warnings not shown)
```

## Вердикт фазы

**PASS.** Все три прибора краснеют на заведомом дефекте своей области, красный
приходит по ожидаемой причине (не по посторонней), и состояние восстанавливается
побайтово с сохранением `HEAD`.

## WARN

1. **Цепочку гейта нельзя прогнать вне контекста задачи.** Первое звено
   (`tdd_gate verify`) вне задачи со статусом `IN_PROGRESS`/`REVIEW` честно
   отдаёт ERROR и останавливает цепочку. Это правильное fail-closed поведение,
   но следствие стоит знать: оператор не может «просто прогнать гейт» на
   `master`, а MUT-04 пришлось ставить на исполнимую часть цепочки
   (`pyrefly && pytest`). Первая редакция пробы этого не учла и измеряла не то,
   что заявляла.
2. **MUT-04 покрывает склейку, но не scoped-rewrite.** Проверено, что красный
   pyrefly останавливает цепочку. НЕ проверено поведение при активном
   scoped-режиме spec-runner, когда пути тестов дописываются в конец строки
   (spec-runner#139): за все пять прогонов пилота scoped ни разу не
   активировался, воспроизвести его здесь нечем.
3. **Пробы прогнаны на `master` после интеграции, а не на `pilot/wave-1`.**
   Протокол требует интеграционную ветку; она удалена по правилу полирепо после
   мержа PR #11, и её роль перешла к `master`. Содержательно это то же
   состояние (`master` == результат интеграции), но буква протокола
   («прогон на интеграционной ветке») выполнена подстановкой, и это стоит
   зафиксировать, а не умолчать.
