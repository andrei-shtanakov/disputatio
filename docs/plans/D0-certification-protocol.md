# D0: протокол сертификации оракула

> Статус: контракт. Исполняется построчно; результат — транскрипт
> `D0-transcript-<phase>-<date>.md`. Параметр `phase = baseline | integration`.
> Будущий `spec-runner bootstrap` (D7-B) обязан воспроизводить наблюдаемое
> поведение транскрипта.
>
> Редакция: 2 (2026-08-08) — исправления по финальному ревью ветки
> d0-baseline; исходная редакция (1) закоммичена в d551952 и исполнена
> baseline-прогоном.

## Категории выхода

| Категория | Значение |
|---|---|
| `OK` | команда завершилась ожидаемо успешно |
| `WARN` | проверка не прошла, но не блокирует фазу |
| `EXPECTED_FAIL` | команда упала именно так, как предписано (assertion выбранного теста) |
| `UNEXPECTED_FAIL` | упало не то или не так (чужие тесты, другой участок) |
| `ERROR` | команда не смогла отработать (import/collection error, окружение) |

Вердикт фазы: все проверки с `blocking = yes` (колонка «Проверки») дают `OK`
(или предписанный `EXPECTED_FAIL` внутри D0-MUT-01); проверки с
`blocking = no` (сейчас только D0-GIT-04) могут быть `WARN` без срыва
вердикта. Любые `WARN` перечисляются в транскрипте явно. Определение вердикта
фазы опирается на колонку `blocking`, а не на прозу.

## Проверки

Формат: `check_id | precondition | command | expected | blocking | categories | remediation`.
Все команды исполняются из корня репо.

Принцип: exit code фиксируется у проверяемой команды, не у последнего
элемента пайпа; пайпы в поле `command` запрещены (или только с явным
`pipefail`).

| check_id | precondition | command | expected | blocking | categories | remediation |
|---|---|---|---|---|---|---|
| D0-GIT-01 | — | `git rev-parse --is-inside-work-tree` | exit 0, `true` → OK | yes | OK\|ERROR | `git init` |
| D0-GIT-02 | D0-GIT-01 | `git rev-list --count HEAD` | exit 0, число ≥ 1 → OK | yes | OK\|ERROR | сделать initial commit |
| D0-GIT-03 | D0-GIT-01 | `git branch --show-current` | exit 0, непустое имя → OK | yes | OK\|ERROR | `git checkout -b <branch>` (detached HEAD не сертифицируется) |
| D0-GIT-04 | D0-GIT-01 | `git remote get-url origin` | exit 0 → OK; exit ≠ 0 → **WARN** (не blocker для локального контура) | no | OK\|WARN | `git remote add origin <url>` |
| D0-GIT-05 | D0-GIT-01 | `git status --porcelain` — пустой вывод | exit 0, пусто → OK | yes | OK\|ERROR | закоммитить/стэшнуть; грязное дерево делает D0-MUT-01 недоказуемым |
| D0-ENV-01 | pyproject.toml существует | `uv sync --dev` | exit 0 → OK | yes | OK\|ERROR | править pyproject / `uv add` |
| D0-TEST-01 | D0-ENV-01 | `uv run pytest -q --collect-only` | exit 0 → OK; exit 2 → ERROR | yes | OK\|ERROR | чинить collection (импорты, синтаксис) |
| D0-TEST-02 | D0-TEST-01 | `uv run pytest -q --collect-only` | в выводе строка `N test(s) collected`, N ≥ 1 → OK; exit 5 / `no tests ran` → ERROR | yes | OK\|ERROR | добавить ≥1 тест |
| D0-TEST-03 | D0-TEST-02 | `uv run pytest -q` | exit 0 → OK; exit 1 → UNEXPECTED_FAIL; exit ≥2 → ERROR | yes | OK\|UNEXPECTED_FAIL\|ERROR | чинить тесты/код до зелёного baseline |
| D0-LINT-01 | D0-ENV-01 | `uv run ruff check .` | exit 0 → OK | yes | OK\|ERROR | `uv run ruff check . --fix`, остаток руками |
| D0-TYPE-01 | D0-ENV-01 | `uv run pyrefly check` | exit 0 → OK | yes | OK\|ERROR | чинить типы |
| D0-SPEC-01 | — | `test -s disputatio-SPEC-001-round-protocol.md` | exit 0 → OK | yes | OK\|ERROR | восстановить спеку из git |

## D0-MUT-01 — mutation_probe (оракул обязан уметь падать)

Precondition: D0-GIT-05 = OK, D0-TEST-03 = OK. Селектор пробы:
`tests/test_smoke.py::test_package_importable`.

Связанность: текст `tests/test_smoke.py` и шаг 2 D0-MUT-01 связаны литералом
`0.1.0`: рефакторинг теста обязан обновить шаг 2, иначе проба тихо
расстыкуется. Следствие: в докстринге теста литерала `0.1.0` быть не должно
— мутация `replace` не должна задевать докстринг.

| шаг | command | expected |
|---|---|---|
| 1. зафиксировать базу | `git rev-parse HEAD` → записать SHA; `git status --porcelain` → пусто | OK |
| 2. сломать assertion | `python3 -c "import pathlib; p = pathlib.Path('tests/test_smoke.py'); p.write_text(p.read_text().replace('0.1.0', '9.9.9'))"` | exit 0; доказательство мутации — `git diff --quiet tests/test_smoke.py` завершается exit 1 (файл изменён); «exit 0 самой мутации» больше не является доказательством |
| 3. подтвердить красный | `uv run pytest -q tests/test_smoke.py::test_package_importable` | exit 1, в выводе `AssertionError` → **EXPECTED_FAIL**; exit ≥2 или падение другого теста → провал пробы (ERROR/UNEXPECTED_FAIL) |
| 4. восстановить | `git checkout -- tests/test_smoke.py` | exit 0 |
| 5. проверить восстановление | `git rev-parse HEAD` == SHA из шага 1 **и** `git status --porcelain` пуст | OK; иначе — провал пробы |
| 6. подтвердить зелёный | `uv run pytest -q tests/test_smoke.py::test_package_importable` | exit 0 → OK |

Phase-заметки: в `phase=integration` (D5) шаги идентичны; дополнительно
требуется совпадение HEAD и dirty-state после восстановления (шаг 5 —
blocking) и прогон на интеграционной ветке, а не на `d0-baseline`.

## Формат транскрипта

Файл `docs/plans/D0-transcript-<phase>-<YYYY-MM-DD>.md`. На каждую проверку —
строка таблицы: `check_id | команда как выполнена | exit code | категория |
заметка`, плюс fenced-блок с сырым выводом для D0-TEST-03, D0-TYPE-01 и всех
шагов D0-MUT-01. В конце — вердикт фазы и список WARN.

## Follow-up (не входит в редакцию 2)

MUT-02 (ruff) / MUT-03 (pyrefly) — mutation_probe по 6-шаговой схеме
D0-MUT-01, применённой к области линта/типов вместо теста. Статус: не
блокирует merge текущего D0 PR; блокирует окончательную спецификацию D7-B;
должен быть закрыт до повторной интеграционной сертификации D5.
