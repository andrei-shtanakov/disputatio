# D0: Baseline-сертификация — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Продуктовый скелет disputatio + исполняемый протокол сертификации
оракула (5-полевые строки, mutation_probe) + транскрипт его прогона
`phase=baseline`.

**Architecture:** Три артефакта: (1) протокол-документ, параметризованный
`phase = baseline | integration` — каждая проверка задаёт precondition,
команду, ожидаемую категорию выхода и remediation; (2) минимальный скелет
`src/disputatio` + один смоук-тест, делающий оракул (`pytest`/`ruff`/`pyrefly`)
живым; (3) транскрипт фактического прогона — будущая acceptance-спека
`spec-runner bootstrap` (D7-B): bootstrap обязан воспроизводить наблюдаемое
поведение транскрипта, а не человеческий текст.

**Tech Stack:** Python 3.12, uv (только uv, никогда pip), pytest, ruff,
pyrefly, hatchling (build backend).

**Контекст ступени:** дизайн — зонтик,
`_cowork_output/plans/2026-08-08-disputatio-battle-stage.md` (§4 D0).

## Global Constraints

- Пакетный менеджер — только `uv` (`uv add`, `uv run`); `uv pip install` и
  `@latest` запрещены.
- Python `>=3.12` (`.python-version` = 3.12).
- Line length 88 (ruff).
- Type hints обязательны; проверка — `uv run pyrefly check`.
- Прозаические документы — на русском (конвенция репо, см. CLAUDE.md).
- Ветка работы: `d0-baseline` от `master` (база — `5bc7963`); в конце — PR,
  **мержит человек**. Прямых коммитов в `master` нет.
- Категории выхода (словарь протокола, совпадает с D4):
  `OK | WARN | EXPECTED_FAIL | UNEXPECTED_FAIL | ERROR`.

---

### Task 1: Ветка + протокол сертификации

**Files:**
- Create: `docs/plans/D0-certification-protocol.md`

**Interfaces:**
- Produces: check_id `D0-GIT-01..05`, `D0-ENV-01`, `D0-TEST-01..03`,
  `D0-LINT-01`, `D0-TYPE-01`, `D0-SPEC-01`, `D0-MUT-01` — Task 3 исполняет их
  дословно; D5 переиспользует с `phase=integration`.

- [x] **Step 1: Создать ветку**

```bash
git -C /Users/Andrei_Shtanakov/labs/disputatio checkout -b d0-baseline master
```

- [x] **Step 2: Записать протокол**

Создать `docs/plans/D0-certification-protocol.md` со следующим содержимым
(дословно; это - контракт, Task 3 исполняет его построчно):

````markdown
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
````

Amendment note: обновлено 2026-08-08 до редакции 2 по итогам финального
ревью; исходная редакция 1 — в d551952.

- [x] **Step 3: Commit**

```bash
git -C /Users/Andrei_Shtanakov/labs/disputatio add docs/plans/D0-certification-protocol.md
git -C /Users/Andrei_Shtanakov/labs/disputatio commit -m "docs(d0): протокол сертификации оракула (baseline|integration, mutation_probe)"
```

---

### Task 2: Продуктовый скелет — TDD от падающего смоука

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_smoke.py`
- Create: `src/disputatio/__init__.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: ветка `d0-baseline` из Task 1.
- Produces: `disputatio.__version__: str == "0.1.0"`; селектор
  `tests/test_smoke.py::test_package_importable` (используется протоколом в
  D0-MUT-01); зелёные `uv run pytest -q`, `uv run ruff check .`,
  `uv run pyrefly check`.

- [x] **Step 1: Добавить dev-зависимости**

```bash
cd /Users/Andrei_Shtanakov/labs/disputatio
uv add --dev pytest ruff pyrefly
```

Ожидаемо: exit 0, появились `[dependency-groups] dev`, `uv.lock`, `.venv`.

- [x] **Step 2: Написать падающий смоук-тест**

Создать `tests/test_smoke.py`:

```python
"""D0 smoke: пакет импортируется, версия согласована с pyproject."""

from disputatio import __version__


def test_package_importable() -> None:
    assert __version__ == "0.1.0"
```

- [x] **Step 3: Убедиться, что тест падает категорией ERROR**

Run: `uv run pytest -q tests/test_smoke.py`
Expected: exit 2, `ModuleNotFoundError: No module named 'disputatio'` —
collection error. Это категория **ERROR** словаря протокола (не
EXPECTED_FAIL!) — зафиксировать различие пригодится в транскрипте.

- [x] **Step 4: Создать пакет и подключить build backend**

Создать `src/disputatio/__init__.py`:

```python
"""disputatio: headless-оркестратор author↔reviewer debate loop (SPEC-001)."""

__version__ = "0.1.0"
```

В `pyproject.toml` заменить `description = "Add your description here"` на
`description = "Headless author↔reviewer debate-loop orchestrator"` и добавить
секции (build backend нужен, чтобы `uv sync` ставил пакет editable и
`import disputatio` работал из тестов; конфиг ruff — line 88 + isort):

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/disputatio"]

[tool.ruff]
line-length = 88

[tool.ruff.lint]
extend-select = ["I"]
```

- [x] **Step 5: Пересинхронизировать окружение и убедиться, что тест зелёный**

```bash
uv sync --dev
uv run pytest -q
```

Expected: `uv sync` exit 0 (пакет установлен editable); pytest exit 0,
`1 passed`.

- [x] **Step 6: Линт**

Run: `uv run ruff check .`
Expected: exit 0, `All checks passed!`. Если нет — `uv run ruff check . --fix`,
остаток руками; `uv run ruff format .` для формата.

- [x] **Step 7: Типы**

```bash
uv run pyrefly init
uv run pyrefly check
```

Expected: `init` добавляет `[tool.pyrefly]` в pyproject (или создаёт конфиг —
принять дефолт); `check` exit 0, 0 errors. Version warnings игнорируются,
если сам check зелёный.

- [x] **Step 8: Дополнить .gitignore кэшами инструментов**

Добавить в конец `.gitignore`:

```
# Tool caches
.pytest_cache/
.ruff_cache/
```

(`.venv`, `__pycache__` уже покрыты.) Проверить: `git status --porcelain` не
показывает `.pytest_cache/`, `.ruff_cache/`, `.venv/`.

- [x] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock tests/test_smoke.py src/disputatio/__init__.py .gitignore
git commit -m "feat(d0): продуктовый скелет — src/disputatio, смоук-тест, pytest+ruff+pyrefly"
```

Внимание: если `pyrefly init` создал отдельный конфиг-файл — добавить и его.

---

### Task 3: Прогон протокола `phase=baseline` + транскрипт

**Files:**
- Create: `docs/plans/D0-transcript-baseline-2026-08-08.md`

**Interfaces:**
- Consumes: протокол из Task 1 (все check_id), скелет из Task 2 (селектор
  `tests/test_smoke.py::test_package_importable`).
- Produces: транскрипт — acceptance-спека для D7-B; вердикт фазы baseline.

- [x] **Step 1: Исполнить проверки D0-GIT-01..D0-SPEC-01 построчно**

Выполнить команды из таблицы протокола дословно, из корня репо, фиксируя для
каждой: команду, exit code (`echo $?` сразу после), категорию по словарю,
заметку. Ожидаемые категории на этой фазе: все OK; D0-GIT-04 = OK (origin
задан). Любое расхождение — остановиться, применить remediation из протокола,
перезапустить проверку, отразить это в заметке (remediation — часть
транскрипта, не позор).

- [x] **Step 2: Исполнить D0-MUT-01 по шагам 1–6**

Дословно по протоколу. Критичное ожидание: шаг 3 — exit 1 с `AssertionError`
(EXPECTED_FAIL), шаг 5 — HEAD не изменился и дерево чистое, шаг 6 — exit 0.
Сырой вывод шагов 3 и 6 — в fenced-блоки.

- [x] **Step 3: Записать транскрипт**

Создать `docs/plans/D0-transcript-baseline-2026-08-08.md` по формату из
протокола: таблица всех проверок, fenced-блоки сырого вывода (D0-TEST-03,
D0-TYPE-01, D0-MUT-01 шаги 3/5/6), итоговый вердикт фазы + явный список WARN
(ожидаемо пустой).

- [x] **Step 4: Commit**

```bash
git add docs/plans/D0-transcript-baseline-2026-08-08.md
git commit -m "docs(d0): транскрипт сертификации phase=baseline — вердикт зелёный"
```

(Если вердикт НЕ зелёный — коммитить транскрипт всё равно, честно, и
остановиться: провал сертификации = blocking finding ступени.)

---

### Task 4: PR + battle-log

**Files:**
- Modify (зонтик): `_cowork_output/battle-log/2026-08-runs.md`

**Interfaces:**
- Consumes: три коммита Task 1–3 на `d0-baseline`.
- Produces: PR в disputatio (мержит человек); запись о прогоне в battle-log.

- [ ] **Step 1: Push + PR**

```bash
cd /Users/Andrei_Shtanakov/labs/disputatio
git push -u origin d0-baseline
gh pr create --title "D0: baseline-сертификация — протокол, скелет, транскрипт" --body "$(cat <<'EOF'
Ступень пилота disputatio, шаг D0 (дизайн: зонтик, `_cowork_output/plans/2026-08-08-disputatio-battle-stage.md` §4).

- Протокол сертификации оракула: 5-полевые проверки + mutation_probe, параметр `phase = baseline | integration` (переиспользуется в D5).
- Продуктовый скелет: `src/disputatio` + смоук-тест; pytest/ruff/pyrefly зелёные.
- Транскрипт `phase=baseline` — будущая acceptance-спека `spec-runner bootstrap` (D7-B).

Мерж — человек (правило ступени).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Отработать ревью PR**

По правилу pr-copilot-review-gate: дождаться замечаний Copilot (если бот
подключён к репо), валидные — починить коммитами в ветку, невалидные —
аргументированно закрыть. Не останавливаться на «PR opened».

- [ ] **Step 3: Запись в battle-log (зонтик)**

Дописать в `/Users/Andrei_Shtanakov/labs/all_ai_orchestrators/_cowork_output/battle-log/2026-08-runs.md`
(append-only) запись по образцу существующих: дата, трек disputatio, шаг D0,
что прогнано (протокол phase=baseline, mutation_probe), вердикт, ссылка на PR,
находки (если были). Закоммитить в корневой репо:

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators
git add _cowork_output/battle-log/2026-08-runs.md
git commit -m "battle-log: disputatio D0 baseline-сертификация"
```

- [ ] **Step 4: Доложить пользователю**

Сообщить: ссылка на PR, вердикт сертификации, находки/фрикции D0 (вход D7-B),
следующий шаг ступени — D1 (ручная decomposition SPEC-001: инварианты +
project.yaml).

## Self-review

Прогнан по чек-листу writing-plans: покрытие §4 D0 дизайн-дока — протокол
(5 полей, phase-параметр, mutation_probe с проверкой SHA/dirty-state), скелет
(src, смоук, pytest+ruff+pyrefly, поверх `5bc7963`), транскрипт как
acceptance D7-B — всё замаплено на Task 1–3; плейсхолдеров нет — содержимое
протокола, теста и конфигов приведено дословно; имена согласованы: селектор
`tests/test_smoke.py::test_package_importable` и `__version__ == "0.1.0"`
одинаковы в Task 1 (протокол), Task 2 (код) и Task 3 (проба).
