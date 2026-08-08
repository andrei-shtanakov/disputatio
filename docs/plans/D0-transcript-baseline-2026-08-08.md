# D0-транскрипт: сертификация оракула, `phase=baseline`, 2026-08-08

> Исполнено построчно по `docs/plans/D0-certification-protocol.md` на ветке
> `d0-baseline`, база HEAD = `c95b46c65cf60f3bbe2d2bd8dd58e2a73c07ba23`, из
> корня репо `/Users/Andrei_Shtanakov/labs/disputatio`. Все команды
> выполнены дословно; фиксируется фактический вывод, не ожидаемый.

Baseline-прогон выполнен по редакции d551952. После прогона протокол
исправлен по review findings; фактические результаты и категории исходного
прогона не переклассифицировались.

## Проверки D0-GIT-01 … D0-SPEC-01

| check_id | команда как выполнена | exit code | категория | заметка |
|---|---|---|---|---|
| D0-GIT-01 | `git rev-parse --is-inside-work-tree` | 0 | OK | вывод `true` |
| D0-GIT-02 | `git rev-list --count HEAD` | 0 | OK | вывод `3` (≥ 1) |
| D0-GIT-03 | `git branch --show-current` | 0 | OK | вывод `d0-baseline` |
| D0-GIT-04 | `git remote get-url origin` | 0 | OK | вывод `git@github.com:andrei-shtanakov/disputatio.git`; origin задан, WARN-ветка словаря не сработала |
| D0-GIT-05 | `git status --porcelain` | 0 | OK | вывод пуст — дерево чистое |
| D0-ENV-01 | `uv sync --dev` | 0 | OK | `Resolved 9 packages`, `Checked 8 packages` |
| D0-TEST-01 | `uv run pytest -q --collect-only` | 0 | OK | 1 тест собран (`tests/test_smoke.py::test_package_importable`) |
| D0-TEST-02 | `uv run pytest -q --collect-only -q \| tail -1` | 0 | **WARN** | см. заметку ниже — вывод пуст, строка `«N tests collected»` из словаря не воспроизводится дословно |
| D0-TEST-03 | `uv run pytest -q` | 0 | OK | `1 passed in 0.00s` |
| D0-LINT-01 | `uv run ruff check .` | 0 | OK | `All checks passed!` |
| D0-TYPE-01 | `uv run pyrefly check` | 0 | OK | `0 errors` |
| D0-SPEC-01 | `test -s disputatio-SPEC-001-round-protocol.md` | 0 | OK | файл непустой |

### Заметка к D0-TEST-02 (WARN)

Команда исполнена дословно, как предписано протоколом (известный нюанс —
пайп с двойным `-q` и `tail -1`). Фактический вывод пайпа — пустая строка,
а не `«N tests collected»`. Причина: второй `-q` переключает pytest в более
тихий режим сводки (`tests/test_smoke.py: 1` вместо `1 test collected in
0.00s`), и pytest печатает эту сводку с завершающей пустой строкой; `tail -1`
захватывает именно эту завершающую пустую строку, а не строку со счётчиком.
Раздельная проверка (`uv run pytest -q --collect-only -q` без пайпа,
перенаправлено в файл) подтверждает: строка `tests/test_smoke.py: 1`
присутствует, за ней — пустая строка, итого 2 строки в выводе; `tail -1`
берёт вторую.

Discrepancy с буквальным ожиданием словаря есть, но она не блокирует фазу:
exit code команды = 0 (не 5, не «no tests ran»), а само наличие ≥1
собранного теста уже независимо подтверждено D0-TEST-02-предшественником
D0-TEST-01 (`1 test collected in 0.00s`, exit 0). Ремедиация протокола
(«добавить ≥1 тест») неприменима — тест уже есть, проблема не в количестве
тестов, а в формате строки, порождаемом самой командой протокола. Категория
зафиксирована как `WARN` (не `OK`, чтобы не подгонять под словарь; не
`ERROR`, так как фаза не заблокирована) и вынесена в friction-вход для D7-B:
воспроизводящий bootstrap должен либо не полагаться на этот буквальный
паттерн строки, либо протокол должен быть скорректирован отдельным
решением — вне рамок этой сертификации.

Сырой вывод (раздельный запуск команды и `tail -1` на неё же, для
прозрачности):

```
$ uv run pytest -q --collect-only -q
tests/test_smoke.py: 1

$ uv run pytest -q --collect-only -q | tail -1
<пусто>
```

## Сырой вывод D0-TEST-03

```
$ uv run pytest -q
.                                                                        [100%]
1 passed in 0.00s
exit=0
```

## Сырой вывод D0-TYPE-01

```
$ uv run pyrefly check
 INFO Checking project configured at `/Users/Andrei_Shtanakov/labs/disputatio/pyproject.toml`
 INFO 0 errors
exit=0
```

## D0-MUT-01 — mutation_probe

Precondition подтверждён: D0-GIT-05 = OK, D0-TEST-03 = OK. Селектор:
`tests/test_smoke.py::test_package_importable`.

| шаг | команда как выполнена | exit code | категория | заметка |
|---|---|---|---|---|
| 1. зафиксировать базу | `git rev-parse HEAD`; `git status --porcelain` | 0; 0 | OK | SHA = `c95b46c65cf60f3bbe2d2bd8dd58e2a73c07ba23`, дерево чистое |
| 2. сломать assertion | `sed -i '' 's/== "0.1.0"/== "9.9.9"/' tests/test_smoke.py` | 0 | OK | файл изменён на `assert __version__ == "9.9.9"` |
| 3. подтвердить красный | `uv run pytest -q tests/test_smoke.py::test_package_importable` | 1 | **EXPECTED_FAIL** | `AssertionError` присутствует в выводе, ровно как предписано |
| 4. восстановить | `git checkout -- tests/test_smoke.py` | 0 | OK | файл восстановлен |
| 5. проверить восстановление | `git rev-parse HEAD`; `git status --porcelain` | 0; 0 | OK | SHA совпал с шагом 1 (`c95b46c65cf60f3bbe2d2bd8dd58e2a73c07ba23`), дерево чистое |
| 6. подтвердить зелёный | `uv run pytest -q tests/test_smoke.py::test_package_importable` | 0 | OK | `1 passed in 0.00s` |

### Сырой вывод шага 1

```
$ git rev-parse HEAD
c95b46c65cf60f3bbe2d2bd8dd58e2a73c07ba23
exit=0

$ git status --porcelain
exit=0
```

### Сырой вывод шага 2

```
$ sed -i '' 's/== "0.1.0"/== "9.9.9"/' tests/test_smoke.py
exit=0
```

Содержимое `tests/test_smoke.py` после шага (дословно, редакция файла на
момент прогона — c95b46c):

```
"""D0 smoke: пакет импортируется, версия согласована с pyproject."""

from disputatio import __version__


def test_package_importable() -> None:
    assert __version__ == "9.9.9"
```

### Сырой вывод шага 3

```
$ uv run pytest -q tests/test_smoke.py::test_package_importable
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
exit=1
```

### Сырой вывод шага 4

```
$ git checkout -- tests/test_smoke.py
exit=0
```

### Сырой вывод шага 5

```
$ git rev-parse HEAD
c95b46c65cf60f3bbe2d2bd8dd58e2a73c07ba23
exit=0

$ git status --porcelain
exit=0
```

SHA до пробы (шаг 1) и после восстановления (шаг 5) идентичны:
`c95b46c65cf60f3bbe2d2bd8dd58e2a73c07ba23`. Дерево в обоих случаях чистое.

### Сырой вывод шага 6

```
$ uv run pytest -q tests/test_smoke.py::test_package_importable
.                                                                        [100%]
1 passed in 0.00s
exit=0
```

## Вердикт фазы `baseline`

**Зелёный.** Все blocking-проверки (D0-GIT-01…03, D0-GIT-05, D0-ENV-01,
D0-TEST-01, D0-TEST-03, D0-LINT-01, D0-TYPE-01, D0-SPEC-01) — `OK`.
D0-GIT-04 — `OK` (origin задан, non-blocking ветка словаря не потребовалась).
D0-MUT-01 отработал по протоколу: шаг 3 дал предписанный `EXPECTED_FAIL`
(`AssertionError`, exit 1), шаг 5 подтвердил идентичный HEAD и чистое дерево
после восстановления, шаг 6 — зелёный прогон (exit 0). Оракул подтверждённо
умеет падать именно предписанным образом и восстанавливаться без следов.

Remediation протокола не применялась ни на одной проверке — расхождений,
требующих remediation, не возникло.

### Список WARN (явно, включая пустой случай)

- `D0-TEST-02` — пайп с двойным `-q` и `tail -1` даёт пустую строку вместо
  буквального `«N tests collected»` (см. заметку выше); не блокирует фазу,
  количество собранных тестов независимо подтверждено `D0-TEST-01`. Это
  friction-вход для D7-B (`spec-runner bootstrap`).

Иных WARN нет.
