# TDD-gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Гейт проверяемого TDD для leaf-задач волны 1: агент создаёт red-чекпоинт
командой `red`, независимый `verify` (внутри `test_command`, ДО commit)
переигрывает red SHA в отдельном worktree и выносит типизированный вердикт;
строгий режим — задача без claim падает.

**Architecture:** Один standalone-скрипт `scripts/tdd_gate.py` (без зависимостей
кроме stdlib — он должен работать в worktree до/вне uv-окружения продукта),
evidence на диске (`spec/.tdd-evidence/{claims,verdicts,waivers}`, append-only),
интеграция через `test_command` (pre-commit enforcement) + `post_done`-плагин
(audit-фоллбэк). Доверие: агент пишет только `claims/`; `verdicts/` и `waivers/`
защищены `harness_guard: strict`. Итоговый default:
`claim текущей задачи → replay → PASS → pytest → commit; нет claim → FAIL;
валидный операторский waiver → WAIVED → pytest; чужой/stale/неоднозначный → ERROR`.

**Tech Stack:** Python 3.12 stdlib (argparse, json, subprocess, pathlib, re),
pytest для тестов самого гейта, git worktree для replay.

**Контекст:** дизайн ступени — зонтик `_cowork_output/plans/2026-08-08-disputatio-battle-stage.md`
(§3, §4 D4); решения владельца — строгий режим, привязка к текущей задаче через
`tasks.md`, ужесточённый red, retry-семантика, waiver = операторское исключение.

## Global Constraints

- Только uv; Python >=3.12; line length 88 (ruff); type hints + `uv run pyrefly check`.
- Проза — на русском. Ветка `tdd-gate` от `master` (`e5dc442`); PR мержит человек.
- `scripts/tdd_gate.py` — только stdlib (никаких pydantic/yaml: скрипт запускается
  из `test_command` до всяких установок и не должен зависеть от продуктовых deps).
- Гейт НЕ импортирует spec-runner (тот установлен как uv tool, не библиотека).
- Категории: `PASS | EXPECTED_FAIL | UNEXPECTED_FAIL | ERROR | WAIVED` — словарь
  совпадает с протоколом D0 (+WAIVED). `WAIVED ≠ PASS`; ось H3 не закрывается
  waived-задачей.
- Файлы PR вне скоупов волны 1 (INV-05/06 не задеты): `scripts/`, `tests/harness/`,
  `spec/**`, `project.yaml`, `docs/plans/`. `pyproject.toml` НЕ трогать.
- Evidence трекается в git (провенанс едет в PR workstream-а). Верификация после
  каждой задачи: `uv run pytest -q tests/harness/ && uv run ruff check . && uv run pyrefly check`.

## Схемы evidence (используются всеми задачами)

`spec/.tdd-evidence/claims/<TASK_ID>.json` (пишет `red`; append-only через
revisions — см. Task 1):

```json
{"schema": "tdd-claim/v1", "task_id": "TASK-001", "selector": "tests/x.py::test_y",
 "expected_behavior": "краткая формулировка", "baseline_sha": "...", "red_sha": "...",
 "created_at": "iso8601", "revision": 1}
```

`spec/.tdd-evidence/verdicts/<TASK_ID>.json` (пишет только `verify`/`audit`):

```json
{"schema": "tdd-verdict/v1", "task_id": "TASK-001", "claim_revision": 1,
 "red_sha": "...", "verified_head": "...", "red_replay": "EXPECTED_FAIL",
 "selector_at_head": "PASS", "verdict": "PASS", "checked_at": "iso8601",
 "notes": ""}
```

`spec/.tdd-evidence/waivers/<TASK_ID>.json` (создаёт ТОЛЬКО оператор, до
запуска задачи; каталог в `harness_files`):

```json
{"schema": "tdd-waiver/v1", "task_id": "TASK-001", "reason": "documentation-only",
 "approved_by": "human", "baseline_sha": "..."}
```

Red-коммит несёт трейлеры (recovery-источник, если запись claim упала):

```
tdd-gate: red checkpoint TASK-001

TDD-Red-Task: TASK-001
TDD-Baseline: <baseline_sha>
TDD-Selector: <selector>
```

---

### Task 1: Ветка, каркас и модуль evidence (модели + атомарный IO)

**Files:**
- Create: `scripts/tdd_gate.py` (начальный каркас: константы, dataclasses, IO)
- Create: `tests/harness/__init__.py` (пустой)
- Create: `tests/harness/test_evidence.py`

**Interfaces:**
- Produces (используют Task 2–5): `Claim`, `Verdict`, `Waiver` (dataclasses,
  `to_json/from_json`), `write_json_atomic(path, obj)` (temp+rename),
  `load_claim(root, task_id) -> Claim | None`, `load_verdict(...)`,
  `load_waiver(...)`, `EVIDENCE = Path("spec/.tdd-evidence")`, категории
  `CAT_PASS/..., GateError(Exception, exit_code=3)`.
- Контракт exit-кодов всего скрипта: `0` OK/PASS/WAIVED, `1` FAIL
  (нет claim / red не подтверждён), `3` ERROR (неоднозначность, чужой claim,
  сломанное окружение). `2` не используем — pytest занял его под collection error.

- [ ] **Step 1: Ветка**

```bash
git -C /Users/Andrei_Shtanakov/labs/disputatio checkout -b tdd-gate master
```

- [ ] **Step 2: Падающий тест модели/IO**

`tests/harness/test_evidence.py`:

```python
"""Evidence-модели и атомарный IO tdd_gate."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate  # noqa: E402


def test_claim_roundtrip(tmp_path: Path) -> None:
    claim = tdd_gate.Claim(
        task_id="TASK-001",
        selector="tests/x.py::test_y",
        expected_behavior="x",
        baseline_sha="a" * 40,
        red_sha="b" * 40,
        created_at="2026-08-08T00:00:00",
        revision=1,
    )
    path = tmp_path / "c.json"
    tdd_gate.write_json_atomic(path, claim.to_json())
    loaded = tdd_gate.Claim.from_json(json.loads(path.read_text()))
    assert loaded == claim


def test_atomic_write_no_partial(tmp_path: Path) -> None:
    path = tmp_path / "v.json"
    tdd_gate.write_json_atomic(path, {"k": "v"})
    assert json.loads(path.read_text()) == {"k": "v"}
    assert list(tmp_path.iterdir()) == [path]  # tmp-файл не оставлен


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert tdd_gate.load_claim(tmp_path, "TASK-404") is None
```

- [ ] **Step 3: Прогнать — падает (нет scripts/tdd_gate.py)**

Run: `uv run pytest -q tests/harness/test_evidence.py`
Expected: collection error / ImportError.

- [ ] **Step 4: Каркас scripts/tdd_gate.py**

Написать: докстринг модуля (назначение + словарь категорий), константы
категорий, `EVIDENCE`, три `@dataclass(frozen=True)` (`Claim`, `Verdict`,
`Waiver`) с `to_json()/from_json()` (обычные dict↔dataclass, поле `schema`
проверяется в `from_json` — несовпадение → `GateError`), `write_json_atomic`
(в тот же каталог: `path.with_suffix(".tmp")` → `os.replace`), `load_*`
(None если файла нет; битый JSON → `GateError`). Все функции типизированы.

- [ ] **Step 5: Зелёный + линт/типы**

```bash
uv run pytest -q tests/harness/ && uv run ruff check . && uv run pyrefly check
```

- [ ] **Step 6: Commit**

```bash
git add scripts/tdd_gate.py tests/harness/
git commit -m "feat(tdd-gate): evidence-модели claim/verdict/waiver + атомарный IO"
```

---

### Task 2: Резолвер текущей задачи из spec/*tasks.md

**Files:**
- Modify: `scripts/tdd_gate.py`
- Create: `tests/harness/test_current_task.py`

**Interfaces:**
- Produces: `resolve_current_task(root: Path) -> str` — сканирует все
  `root/spec/*tasks.md`; задача «текущая», если её meta-строка содержит
  `IN_PROGRESS` или `REVIEW` (эмодзи 🔄/🔍 или plain-текст — оба формата
  spec-runner). Ровно одна по всем файлам → её ID; ноль или больше одной →
  `GateError`. Формат строк spec-runner: заголовок задачи —
  `### TASK-NNN: ...` (уровень #### тоже допустим), meta-строка ниже содержит
  `| <emoji> STATUS`.

- [ ] **Step 1: Падающие тесты**

`tests/harness/test_current_task.py` — минимум эти случаи (фикстура пишет
`tmp_path/spec/tasks.md` с заданным содержимым):

```python
"""Резолвер текущей задачи: ровно один IN_PROGRESS/REVIEW по всем spec/*tasks.md."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate  # noqa: E402


def write_tasks(root: Path, name: str, body: str) -> None:
    spec = root / "spec"
    spec.mkdir(exist_ok=True)
    (spec / name).write_text(body)


ONE_RUNNING = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
"""


def test_single_in_progress(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_maestro_prefix_file(tmp_path: Path) -> None:
    write_tasks(tmp_path, "maestro-tasks.md", ONE_RUNNING)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_review_status_counts(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "🔍 REVIEW"))
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_plain_format_without_emoji(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "IN_PROGRESS"))
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_zero_running_is_error(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "⬜ TODO"))
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_two_running_is_error(tmp_path: Path) -> None:
    body = ONE_RUNNING.replace("⬜ TODO", "🔄 IN_PROGRESS")
    write_tasks(tmp_path, "tasks.md", body)
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_two_files_one_running_each_is_error(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING)
    write_tasks(tmp_path, "maestro-tasks.md", ONE_RUNNING.replace("TASK-00", "KAP-00"))
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)
```

- [ ] **Step 2: Прогнать — падают** (`resolve_current_task` нет)

- [ ] **Step 3: Реализация**

Регексы: заголовок `^#{2,6}\s+([A-Z][A-Z0-9]*-\d+)\b`; статус в строке —
`IN_PROGRESS|REVIEW` словом (без DONE/TODO/BLOCKED; регистронезависимо, но
только как отдельное слово). Привязка статуса к последнему встреченному
заголовку задачи. Дубли одного ID в двух файлах с running-статусом — тоже
`GateError`.

- [ ] **Step 4: Зелёный + линт/типы; Commit**

```bash
uv run pytest -q tests/harness/ && uv run ruff check . && uv run pyrefly check
git add -u && git add tests/harness/test_current_task.py
git commit -m "feat(tdd-gate): резолвер текущей задачи из spec/*tasks.md"
```

---

### Task 3: Git-хелперы: baseline, классификация изменений, red-коммит, recovery

**Files:**
- Modify: `scripts/tdd_gate.py`
- Create: `tests/harness/test_git_ops.py`
- Create: `tests/harness/conftest.py` (фикстура tmp-git-репо)

**Interfaces:**
- Produces: `git(root, *args) -> str` (subprocess, check, strip);
  `head_sha(root)`; `changed_paths(root) -> list[str]` (union: staged + unstaged
  + untracked, `git status --porcelain -z`);
  `classify_changes(paths, task_id) -> tuple[list[str], list[str]]` — (allowed,
  forbidden): allowed = пути под `tests/` + claims-файл текущей задачи
  (`spec/.tdd-evidence/claims/<task_id>.json`) + **runner-owned** правка
  `spec/*tasks.md` и `spec/.task-history.log`/`spec/.*task-history.log`
  (spec-runner уже пометил задачу in_progress до старта агента — это ожидаемо);
  всё прочее — forbidden;
  `commit_red(root, task_id, baseline, selector) -> str` — коммитит ТОЛЬКО
  pathspec `tests/` (`git add -- tests/` + `git commit -- tests/`; никакого
  `-A`; `spec/tasks.md` в red-коммит не попадает) с трейлерами из шапки плана,
  возвращает SHA;
  `find_red_commit_by_trailer(root, task_id) -> str | None` — recovery: ищет в
  `git log --format` последний коммит с `TDD-Red-Task: <task_id>`.

**Фикстура** `tests/harness/conftest.py`:

```python
"""Фикстура: временный git-репо со скелетом под тесты гейта."""

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    def run(*args: str) -> None:
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "master")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "spec").mkdir()
    (tmp_path / "tests" / "test_seed.py").write_text("def test_seed():\n    assert True\n")
    (tmp_path / "src" / "mod.py").write_text("X = 1\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")
    return tmp_path
```

- [ ] **Step 1: Падающие тесты** — минимум: `changed_paths` видит staged,
unstaged и untracked; `classify_changes` относит `tests/test_new.py` в allowed,
`src/mod.py` в forbidden, `spec/tasks.md` и `spec/.task-history.log` в allowed,
`spec/.tdd-evidence/claims/TASK-001.json` в allowed для TASK-001 и в forbidden
для TASK-002; `commit_red` создаёт коммит, в котором есть tests/-файл, НЕТ
spec/tasks.md, message содержит все три трейлера; `find_red_commit_by_trailer`
находит его SHA и возвращает None в репо без red-коммитов. (Тесты писать
конкретными, по образцу Task 2; каждая проверка — отдельный test_*.)

- [ ] **Step 2: Прогнать — падают. Step 3: Реализация. Step 4: Зелёный
+ линт/типы; Commit** `feat(tdd-gate): git-хелперы red-коммита и классификации изменений`

---

### Task 4: Команда `red`

**Files:**
- Modify: `scripts/tdd_gate.py`
- Create: `tests/harness/test_red.py`

**Interfaces:**
- Produces: `cmd_red(root, selector, expected_behavior) -> int` (и CLI
  `red -k <selector> [-m <expected_behavior>]`). Логика строго по порядку:
  1. `task_id = resolve_current_task(root)`;
  2. существующий pending claim этого task_id (claim без PASS/WAIVED-вердикта):
     если его `red_sha` существует в истории — **идемпотентный выход 0**
     («red checkpoint уже создан, продолжай от него»), второй red-коммит НЕ
     создаётся; если claim есть, а коммита нет — `GateError` (recovery-required);
  3. чужой pending claim (другого task_id) → `GateError`;
  4. `classify_changes`: forbidden непуст → exit 1 с перечислением (product-код
     менять до red нельзя);
  5. запуск селектора: `uv run pytest -q <selector>`; классификация:
     exit 1 И `AssertionError` в выводе И падение именно селектора →
     EXPECTED_FAIL; exit 0 → FAIL («тест не падает — он ничего не доказывает»);
     иначе → ERROR (collection/import/окружение);
  6. `commit_red(...)` → red_sha;
  7. запись claim (revision = прошлый+1 при supersession — но supersession в
     v1 запрещён: существующий claim с PASS-вердиктом и новым red — это
     `GateError`, см. retry-семантику);
  8. recovery-ветка: если на шаге 7 запись упала, следующий запуск `red`
     восстанавливает claim из трейлеров red-коммита
     (`find_red_commit_by_trailer`) — тест обязателен.

- [ ] **Step 1: Падающие тесты** — минимум: happy path (создаёт red-коммит +
claim, exit 0); повторный вызов — идемпотентен (коммит один); изменённый
`src/**` до red → exit 1; зелёный селектор → exit 1; сломанный импорт → exit 3;
нет in_progress-задачи → exit 3; claim без red-коммита → восстановление из
трейлера, если коммит есть, иначе exit 3. Фикстура: `repo` из Task 3 +
`write_tasks` из Task 2 (вынести хелпер в conftest). Селектор в тестах —
реальный мини-тест в tmp-репо (`tests/test_feature.py::test_new`), падающий
assertion'ом на seed-состоянии.

- [ ] **Step 2: Прогнать — падают. Step 3: Реализация. Step 4: Зелёный
+ линт/типы; Commit** `feat(tdd-gate): команда red — checkpoint с идемпотентностью и recovery`

---

### Task 5: Команда `verify` (+ waiver)

**Files:**
- Modify: `scripts/tdd_gate.py`
- Create: `tests/harness/test_verify.py`

**Interfaces:**
- Produces: `cmd_verify(root) -> int` (CLI `verify`). Строго по решению
  владельца:
  1. `task_id = resolve_current_task(root)`;
  2. claim для task_id отсутствует → проверка waiver: валидный
     (`schema`, `task_id` совпадает, `approved_by == "human"`, `baseline_sha`
     — предок HEAD) → пишем verdict `WAIVED`, exit 0; невалидный/нет →
     **exit 1 (fail-closed)**;
  3. pending claims ДРУГИХ task_id при живом собственном → не мешают (они
     чужая история); >1 claim-файла для ОДНОГО task_id невозможно по именованию,
     но claim с revision-конфликтом или несовместимый с verdict → exit 3;
  4. существующий verdict PASS: тот же `red_sha` и тот же
     `verified_head == HEAD` → идемпотентный PASS, exit 0; HEAD изменился →
     полная переверификация (verdict перезаписывается новым, старый уходит в
     `verdicts/<TASK>.history.jsonl` — append-only revisioning); несовместимый
     (verdict.red_sha ≠ claim.red_sha) → exit 3;
  5. цепочка: red_sha существует и является предком HEAD или == HEAD; diff
     `baseline..red_sha` касается только `tests/**`; нарушение → exit 3;
  6. **replay**: `git worktree add <tmp> <red_sha>` (tmp вне репо, cleanup в
     finally: `git worktree remove --force`), в worktree
     `uv run pytest -q <selector>`; ожидание — exit 1 + `AssertionError` +
     падение именно селектора → red_replay=EXPECTED_FAIL; иначе
     UNEXPECTED_FAIL/ERROR → вердикт FAIL/ERROR соответственно (exit 1/3);
  7. селектор на текущем дереве: `uv run pytest -q <selector>` → PASS требуется
     (exit 0); красный → exit 1 («реализация не закрывает свой тест» — полный
     suite и так прогонит pytest после гейта в test_command);
  8. запись verdict PASS (write_json_atomic) → exit 0.
- Produces также: `cmd_audit(root) -> int` (для post_done-плагина): для КАЖДОЙ
  задачи со статусом DONE в spec/*tasks.md, у которой есть claim, — verdict
  обязан существовать и быть PASS/WAIVED; нарушение → exit 3. Ничего не
  переигрывает (дёшево, идемпотентно).

- [ ] **Step 1: Падающие тесты** — матрица минимум: happy path (red из Task 4 →
реализация в src → verify PASS, verdict записан); нет claim → exit 1;
нет claim + валидный waiver → exit 0 и verdict WAIVED; waiver с чужим task_id →
exit 1; идемпотентный PASS (повторный verify без изменений — exit 0, verdict
не дублируется); HEAD сдвинулся после PASS → переверификация + history-файл;
подделка verdict (руками записан PASS с чужим red_sha) → exit 3; red_sha не
предок HEAD → exit 3; diff baseline..red трогает src → exit 3; replay даёт
зелёный селектор на red SHA (реализация была уже в red-коммите) →
UNEXPECTED_FAIL → exit 1; audit: DONE-задача с claim без verdict → exit 3.
Replay-тесты используют настоящие git worktree в tmp-репо.

- [ ] **Step 2: Прогнать — падают. Step 3: Реализация (+ argparse main:
`red|verify|audit`, все ошибки GateError → stderr + exit 3). Step 4: Зелёный
+ линт/типы; Commit** `feat(tdd-gate): verify — replay red SHA в worktree, waiver, идемпотентность; audit`

---

### Task 6: Интеграция: плагин, constitution, project.yaml

**Files:**
- Create: `spec/plugins/tdd-gate/plugin.yaml`
- Create: `spec/constitution.md`
- Modify: `project.yaml`

**Interfaces:**
- Consumes: `cmd_audit` (Task 5) как post_done-хук.

- [ ] **Step 1: plugin.yaml**

```yaml
name: tdd-gate
description: "Audit-фоллбэк TDD-гейта: каждая DONE-задача с claim имеет verdict"
hooks:
  post_done:
    command: "uv run python scripts/tdd_gate.py audit"
    blocking: true
    run_on: on_success
```

- [ ] **Step 2: constitution.md** — authoring policy (обучение, НЕ enforcement),
дословно этот текст:

```markdown
# Constitution — disputatio, волна 1

## TDD-дисциплина (обязательна для каждой leaf-задачи)

1. Сначала тест: выбери МИНИМАЛЬНЫЙ тест, доказывающий новое поведение задачи.
   Regression-тест фиксирует наблюдаемое поведение; speculative-тест на
   несуществующие требования не пиши.
2. До реализации выполни: `uv run python scripts/tdd_gate.py red -k <селектор>`
   — гейт проверит, что тест падает именно assertion'ом, и создаст
   red-чекпоинт. Без подтверждённого red задача НЕ завершится (test_command
   упадёт).
3. Только затем пиши реализацию. Менять assertion ради зелёного ЗАПРЕЩЕНО:
   если тест оказался неверным — объясни это в отчёте и начни red заново
   осознанно.
4. `scripts/tdd_gate.py`, `spec/plugins/`, `spec/.tdd-evidence/verdicts/`,
   `spec/.tdd-evidence/waivers/` — НЕ редактировать (harness; правки блокируют
   задачу).
5. Гейт и полный suite запускаются автоматически после задачи; локально можно
   проверить себя: `uv run python scripts/tdd_gate.py verify && uv run pytest -q`.
```

- [ ] **Step 3: project.yaml** — в блоке `spec_runner` заменить
`test_command: "uv run pytest -q"` на:

```yaml
  test_command: "uv run python scripts/tdd_gate.py verify && uv run pytest -q"
```

и добавить в конец блока `spec_runner`:

```yaml
  extra_executor_config:
    executor:
      harness_guard: strict
      harness_files:
        - scripts/tdd_gate.py
        - spec/plugins/tdd-gate
        - spec/.tdd-evidence/verdicts
        - spec/.tdd-evidence/waivers
```

(Буквальные пути, без glob — harness.py спec-runner glob не поддерживает.
Точная структура overlay: ключи под `executor:` — сверить с
`SpecRunnerConfig.to_executor_config()` спек-раннера; если overlay кладётся
на верхний уровень без `executor:`-обёртки — поправить по факту, критерий:
сгенерированный конфиг содержит `harness_guard: strict`.)

- [ ] **Step 4: Каталоги evidence** — создать
`spec/.tdd-evidence/{claims,verdicts,waivers}/.gitkeep` (пустые каталоги git не
трекает, а harness-снапшот должен видеть путь).

- [ ] **Step 5: Валидация + Commit**

```bash
cd /Users/Andrei_Shtanakov/labs/disputatio && maestro validate project.yaml 2>&1 | tail -3
uv run pytest -q tests/harness/ && uv run ruff check . && uv run pyrefly check
git add spec/ project.yaml && git commit -m "feat(tdd-gate): интеграция — plugin audit, constitution, test_command + harness_guard"
```

Expected: validate 0 errors (scope-no-match допустимы).

---

### Task 7: Смоук на живом spec-runner + PR

**Files:** отчёт в PR body; battle-log (зонтик).

- [ ] **Step 1: Ручной смоук гейта против НАСТОЯЩЕГО spec-runner** (не мок):
во временном клоне disputatio (вне рабочего чекаута, например
`/private/tmp/.../tdd-gate-smoke`) создать `spec/tasks.md` с одной задачей,
пометить её `🔄 IN_PROGRESS` руками, сыграть роль агента: написать падающий
тест → `red` → реализация → `uv run python scripts/tdd_gate.py verify && uv
run pytest -q` (= будущий test_command) → PASS; затем негативный прогон:
новая задача без claim → verify exit 1. Зафиксировать транскрипт смоука в
PR body. (Полный прогон через `spec-runner run` не требуется — это D3;
смоук проверяет контракт test_command.)
- [ ] **Step 2: Push + PR** (`gh pr create`, body: состав, решения владельца —
строгий режим, привязка к tasks.md, retry-семантика, waiver-правила, WAIVED≠PASS,
смоук-транскрипт; мерж — человек).
- [ ] **Step 3: Copilot-ревью** — отработать по правилу (валидное чинить, невалидное
аргументировать).
- [ ] **Step 4: Battle-log** (зонтик, append-only): строка `tdd-gate` — что
построено, решения, находки. Отдельный коммит в зонтике.

## Self-review

Прогнан: покрытие решений владельца — строгий no-claim→FAIL (Task 5.2), привязка
через единственный IN_PROGRESS/REVIEW (Task 2), ужесточённый red с
runner-owned-исключением для tasks.md и pathspec-коммитом без -A (Task 3/4),
трейлеры + recovery (Task 3/4), retry-семантика: идемпотентный red, запрет
второго red-коммита, supersession → ERROR (Task 4), append-only verdicts через
history.jsonl (Task 5.4), waiver операторский с WAIVED≠PASS и baseline-предком
(Task 5.2), тесты retry/stale/multiple/fake-verdict/missing-claim/
runner-modified-tasks.md (Task 4/5 Step 1), plugin=audit-only (Task 6),
literal harness paths (Task 6.3). Типы/имена сквозные: `Claim/Verdict/Waiver`,
`resolve_current_task`, `commit_red`, `cmd_red/cmd_verify/cmd_audit` —
согласованы между задачами. Открытая проверка, вынесенная в Task 6.3 явно:
точная форма `extra_executor_config`-overlay сверяется по факту с
`to_executor_config()` — критерий записан.
