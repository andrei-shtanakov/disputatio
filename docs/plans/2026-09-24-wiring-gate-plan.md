# План имплементации: гейт покрытия `disp gate wiring`

> **Для agentic-исполнителей:** REQUIRED SUB-SKILL:
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans — задача за задачей, шаги — чекбоксы `- [ ]`.

**Цель:** отгрузить дополнительный doc-гейт `disp gate wiring`. Он
перечисляет в текущем `src/` нарушения машиночитаемо объявленных в спеке
инвариантов двух видов и требует, чтобы каждое было структурно связано с
задачей плана.

**Архитектура:** два новых модуля пакета `verifier` и подкоманда CLI.
`wiring_snapshot` владеет git: отпечаток дерева, чистота, чтение блобов.
`wiring` — чистые функции над байтами файлов снимка и текстом документов:
разбор блоков, разрешение имён, два правила, покрытие, находки. `cli.py`
связывает их и отображает результат в коды `0/1/2`. Ядро, runner,
`DocVerifier`, `run_gate` и схема конфига пайплайна не меняются: гейт
подключается штатным `[[pipeline.gates]]`.

**Стек:** Python 3.12+, `ast`, `tomllib`, `subprocess` (git), pytest, uv,
pyrefly, ruff.

**Спека:** `docs/specs/2026-09-24-wiring-gate-design.md`. План аргументирует
от неё; исполнитель читает оба документа. Ссылки вида «§N» ниже — на
разделы спеки.

## Глобальные ограничения

- Пакетный менеджер — только `uv`; тесты — `uv run pytest -q`.
- После каждой задачи: `uv run ruff format . && uv run ruff check .`,
  `uv run pyrefly check`, полный suite зелёный. Строка ≤ 88.
- **Не редактируются ни в одной задаче:** `src/disputatio/core/**`,
  `src/disputatio/runtime/**`, `src/disputatio/verifier/doc_verifier.py`,
  `src/disputatio/verifier/runner.py`, `src/disputatio/verifier/capture.py`.
  Правка в них = выход за границу спеки («без изменения ядра и runner»).
- Пакет `verifier` не импортирует `runtime` и не вызывает агентские CLI
  (INV-10). Сторож `tests/verifier/test_no_agent_cli.py` сканирует
  **текст** каждого модуля пакета (сканер —
  `tests/verifier/agent_cli_scanner.py`): запрещены токены
  `FORBIDDEN_CLI_TOKENS` (`claude`, `codex`, `anthropic` и др.) в любом
  месте, включая докстринги и сообщения, и импорты `disputatio.*` вне
  `disputatio.contracts`/`disputatio.verifier`. Новые модули обязаны
  пройти его без правки сканера: ни в коде, ни в прозе этих слов нет.
- Гейт read-only: ни один модуль не пишет в рабочее дерево, индекс или
  `.git`. Вызовы git — только `rev-parse`, `status`, `ls-tree`, `cat-file`.
- Ошибки ввода гейта — собственное исключение `WiringInputError` пакета
  `verifier` (не `DisputatioError`: `verifier` не импортирует `runtime`).
  CLI ловит его и отдаёт код `2`.
- TDD: red-тест до реализации, коммит после каждой задачи:
  `feat(verifier): …`, `feat(cli): …`, `test(verifier): …`, `docs: …`.
- Русский язык докстрингов и сообщений — как в существующем коде.
- **«Проверяется» = мутация краснеет.** Для каждой fail-closed ветки
  (код `2`, `unverifiable`, `dirty-src`, `stale-snapshot`) тест обязан
  краснеть, если ветку заменить на успех. Исполнитель прогоняет такую
  мутацию хотя бы для одной ветки каждого вида и пишет её результат в
  отчёт задачи.

---

### Задача 1: verifier — снимок кода через git

Реализует §4: отпечаток, чистота, чтение дерева.

**Файлы:**
- Create: `src/disputatio/verifier/wiring_snapshot.py`
- Create: `tests/verifier/test_wiring_snapshot.py`

**Интерфейс:**

```python
class WiringInputError(Exception):
    """Непригодные входы гейта: код 2 (§6)."""

@dataclass(frozen=True, slots=True)
class Snapshot:
    tree: str                      # sha дерева HEAD:<src>
    files: Mapping[str, bytes]     # путь от корня репо -> байты блоба, только .py

def src_fingerprint(repo_root: Path, src: str) -> str: ...
def dirty_src_paths(repo_root: Path, src: str) -> tuple[str, ...]: ...
def read_snapshot(repo_root: Path, src: str) -> Snapshot: ...
```

`WiringInputError` объявляется здесь, потому что первым он нужен снимку;
`wiring.py` импортирует его отсюда.

- [ ] **Шаг 1: red-тесты** на временных git-репозиториях (фикстура
  `tmp_path` + `git init`, `git -c user.name=t -c user.email=t@t commit`):
  - отпечаток равен `git rev-parse HEAD:src`;
  - коммит, меняющий только `docs/x.md`, отпечаток не меняет;
  - `dirty_src_paths` непуст для каждого из трёх случаев по отдельности:
    unstaged-правка, staged-правка (`git add`), untracked-файл в `src/`;
    пуст для правки вне `src/` и для игнорируемого файла в `src/`;
  - `read_snapshot` отдаёт ровно `.py`-файлы дерева `HEAD:src` с байтами из
    git, а не с диска: незакоммиченная правка файла в выдачу не попадает;
  - `WiringInputError` для: каталога без `.git`, репозитория без коммитов,
    отсутствующего `src` в `HEAD`.
- [ ] **Шаг 2: реализация.** `git -C <root> rev-parse HEAD:<src>`;
  `git status --porcelain --untracked-files=all -- <src>`;
  `git ls-tree -r -z <tree>` → блобы `.py` → один
  `git cat-file --batch` с разбором заголовков `<sha> blob <size>`.
  Любой ненулевой код git → `WiringInputError` с текстом stderr.
  `subprocess.run(..., check=False, capture_output=True)`, без shell.
- [ ] **Шаг 3:** мутация — `dirty_src_paths` возвращает `()` всегда →
  тесты чистоты краснеют; результат записать в отчёт.
- [ ] **Шаг 4:** ruff, pyrefly, полный suite; коммит
  `feat(verifier): снимок src для гейта wiring`.

---

### Задача 2: verifier — разбор блоков правил и покрытия

Реализует §3.1, §5.1 и разметку задач плана из §5.3.

**Файлы:**
- Create: `src/disputatio/verifier/wiring.py` (первая часть: модели и разбор)
- Create: `tests/verifier/test_wiring_blocks.py`

**Интерфейс:**

```python
@dataclass(frozen=True, slots=True)
class ConstructRule:
    id: str
    class_module: str      # "disputatio.runtime.pipeline_runner"
    class_name: str        # "ArchitecturalDefectPolicy"
    allowed: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class EnumerateRule:
    id: str
    module: str            # путь от корня репо
    function: str          # qualname
    checkers: tuple[str, ...]
    members: tuple[str, ...]

Rule = ConstructRule | EnumerateRule

@dataclass(frozen=True, slots=True)
class Cover:
    rule: str
    site: str              # "path:line"
    member: str | None
    task: int

@dataclass(frozen=True, slots=True)
class CoverBlock:
    src_tree: str
    covers: tuple[Cover, ...]

def parse_rules(spec_text: str) -> tuple[Rule, ...]: ...
def parse_cover(plan_text: str) -> CoverBlock: ...
def task_sections(plan_text: str) -> Mapping[int, str]: ...
```

- [ ] **Шаг 1: red-тесты.**
  - пример правил §3.1 разбирается в два правила с ожидаемыми полями;
  - пример покрытия §5.1 разбирается;
  - `WiringInputError` для каждого случая: блока нет; два блока; битый
    TOML; неизвестный ключ правила; неизвестный `kind`; нет обязательного
    поля; пустые `allowed`/`checkers`/`members`; повтор `id`; `id` вне
    грамматики (в том числе `P10-policy` с заглавной); `class` без `:` или
    с точкой в qualname; `member` у `construct-only-in`; нет `member` у
    `enumerates-all`; `task` < 1 или не целое; нет `src_tree`;
  - блок с info-строкой `toml` не читается (блок правил не найден →
    ошибка), fenced-блок внутри другого fenced-блока не читается;
  - `task_sections`: заголовки `### Задача 3:` и `### Task 3:` оба
    распознаются; раздел кончается на следующем заголовке уровня 1–3, а
    `####` его не обрывает; два заголовка с одним номером →
    `WiringInputError`.
- [ ] **Шаг 2: реализация.** Fenced-блоки ищутся построчно: открывающая
  строка ```` ``` ```` с info-строкой ровно `disputatio-wiring` /
  `disputatio-wiring-cover`, закрывающая — ```` ``` ````. Разбор —
  `tomllib.loads`, затем явная сверка множеств ключей (закрытая схема).
- [ ] **Шаг 3:** ruff, pyrefly, suite; коммит
  `feat(verifier): разбор деклараций гейта wiring`.

---

### Задача 3: verifier — индекс модулей и разрешение имён

Реализует §3.4 и нарушения `construct-only-in` §3.2.

**Файлы:**
- Modify: `src/disputatio/verifier/wiring.py`
- Create: `tests/verifier/test_wiring_resolve.py`

**Интерфейс:**

```python
@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    kind: str              # "construct-only-in" | "enumerates-all"
    site: str              # "path:line"
    member: str | None
    count: int             # число вызовов на строке; 1 для enumerates-all

def build_index(snapshot: Snapshot, src: str) -> ModuleIndex: ...
def construct_violations(index: ModuleIndex, rule: ConstructRule) -> list[Violation]: ...
```

`ModuleIndex` — модульное имя → (путь, AST, привязки). Строится один раз на
весь снимок.

- [ ] **Шаг 1: red-тесты** на синтетических деревьях (хелпер собирает
  `Snapshot` из словаря «путь → текст», git не нужен):
  - прямой вызов в модуле определения; `from a import C`; `from a import C
    as D`; `import a; a.C()`; `import a.b as m; m.C()`; реэкспорт через
    `pkg/__init__.py`; цепочка реэкспортов; модульный алиас в пакете
    (`pkg/__init__.py`: `from pkg import impl as alias`, вызов
    `pkg.alias.C()`) — каждое даёт нарушение;
  - импорт внутри функции даёт привязку;
  - одноимённый класс `C` в другом модуле и внешний `C` нарушения не дают;
  - вызов в файле из `allowed` не нарушение;
  - `tests/` вне `--src` не анализируется;
  - два вызова на одной строке → одно нарушение, `count=2`;
  - `C().method()` → нарушение по внутреннему вызову;
  - цикл реэкспорта `a ↔ b` без класса не зацикливается и нарушения не
    даёт; цикл, из которого класс достижим другой ветвью, даёт нарушение;
  - вызовы через одну привязку в двух функциях дают два нарушения (стек
    обхода не копится между запросами);
  - `WiringInputError`: модуля класса нет; класса нет на верхнем уровне;
    путь `allowed` отсутствует; `from <модуль снимка> import *`;
    относительный импорт за корень; файл снимка не разбирается `ast.parse`;
    файл с недекодируемыми байтами.
- [ ] **Шаг 2: реализация** строго по §3.4: привязки — список источников на
  имя; `resolve`/`member` возвращают множества; ключ обхода
  `(операция, модуль, имя)`; активный стек с удалением при возврате; новый
  стек на каждый `func`. `ast.parse(блоб_байты, filename=путь)` — кодировка
  по PEP 263 делает сам `ast`; `SyntaxError`/`ValueError` →
  `WiringInputError` с именем файла.
- [ ] **Шаг 3:** мутация — отсечение циклов по накопленному множеству
  вместо активного стека → тест модульного алиаса краснеет; записать.
- [ ] **Шаг 4:** ruff, pyrefly, suite; коммит
  `feat(verifier): правило construct-only-in`.

---

### Задача 4: verifier — правило `enumerates-all`

Реализует §3.5.

**Файлы:**
- Modify: `src/disputatio/verifier/wiring.py`
- Create: `tests/verifier/test_wiring_enumerates.py`

**Интерфейс:**

```python
@dataclass(frozen=True, slots=True)
class Unverifiable:
    rule: str
    site: str
    reason: str

def enumerate_violations(
    index: ModuleIndex, rule: EnumerateRule
) -> list[Violation] | Unverifiable: ...
```

- [ ] **Шаг 1: red-тесты.**
  - тело формы `_guard_history` на снимке `23c9297` (воспроизвести
    синтетически: докстринг + четыре проверяющих вызова, один
    многострочный) с `members` из §3.1 → одно нарушение `doc_sessions` на
    строке `def`;
  - все члены проверены → нарушений нет; лишний проверяемый член нарушения
    не даёт;
  - имя члена только в докстринге, только строковым аргументом, только во
    вложенной функции, только под `if` → член не засчитан (под `if` и
    вложенная функция дают `Unverifiable` целиком);
  - `Unverifiable` для: цикла `for` по источнику (форма после фикса);
    присваивания; `return`; вызова не из `checkers`; проверяющего вызова
    без атрибутного аргумента; вызова с двумя разными `attr`;
  - `Class.method` находится по qualname; функция не найдена →
    `WiringInputError`.
- [ ] **Шаг 2: реализация** по §3.5: параметры функции — `args`,
  `posonlyargs`, `kwonlyargs`, `vararg`, `kwarg`.
- [ ] **Шаг 3:** мутация — «прочий оператор» пропускается вместо
  `Unverifiable` → тест формы после фикса краснеет; записать.
- [ ] **Шаг 4:** ruff, pyrefly, suite; коммит
  `feat(verifier): правило enumerates-all`.

---

### Задача 5: verifier — покрытие и находки

Реализует §5.2, §5.3 и порядок/форму вывода §6.

**Файлы:**
- Modify: `src/disputatio/verifier/wiring.py`
- Create: `tests/verifier/test_wiring_coverage.py`

**Интерфейс:**

```python
@dataclass(frozen=True, slots=True)
class Finding:
    code: str              # uncovered | duplicate-cover | dangling-cover |
                           # missing-task | site-not-in-task | unverifiable |
                           # stale-snapshot | dirty-src
    rule: str
    site: str
    member: str | None
    detail: str

@dataclass(frozen=True, slots=True)
class WiringReport:
    violations: tuple[Violation, ...]
    findings: tuple[Finding, ...]

def check_wiring(
    *, spec_text: str, plan_text: str, snapshot: Snapshot, src: str,
    dirty: tuple[str, ...],
) -> WiringReport: ...

def render_report(report: WiringReport) -> list[str]: ...
```

`check_wiring` — точка сборки: разбор блоков → при `dirty` находки
`dirty-src` и выход → при несовпадении `src_tree` с `snapshot.tree` находка
`stale-snapshot` и выход → индекс → нарушения обоих правил → покрытие.

- [ ] **Шаг 1: red-тесты** (синтетические деревья и планы):
  - каждая из пяти находок §5.3 порождается своим минимальным входом;
  - `dangling-cover` и для места без нарушения, и для необъявленного
    `rule`;
  - `site-not-in-task`: задача есть, `site` в другом разделе; для
    `enumerates-all` — `site` в разделе есть, `member` нет;
  - `dirty` непуст → только `dirty-src`, покрытие не вычисляется;
    отпечаток расходится → только `stale-snapshot`;
  - `Unverifiable` → находка `unverifiable`; запись покрытия на это место
    — `dangling-cover`, а не покрытие;
  - `render_report`: строки отсортированы по `(код, rule, site, member)`,
    последняя — сводка `wiring: N нарушений, M покрыто, K находок`;
    `count` печатается у `construct-only-in`.
- [ ] **Шаг 2: реализация.** Ключ покрытия — `(rule, site)` или
  `(rule, site, member)`; сопоставление — словари по ключу; ссылка в
  разделе — вхождение строки `site` (и `member`) в текст раздела.
- [ ] **Шаг 3:** ruff, pyrefly, suite; коммит
  `feat(verifier): покрытие и находки гейта wiring`.

---

### Задача 6: cli — подкоманда `disp gate wiring`

Реализует §2, §6 и точку подключения §2.1.

**Файлы:**
- Modify: `src/disputatio/cli.py` (подпарсер `gate` → `wiring`, обработчик)
- Create: `tests/cli/test_cli_gate_wiring.py`

- [ ] **Шаг 1: red-тесты** через `main([...])` на временном git-репо:
  - всё покрыто → код `0`, stdout кончается сводкой;
  - непокрытое нарушение → код `1`, строка `uncovered …`;
  - каждая причина кода `2` из таблицы §6 хотя бы одним случаем, включая
    отсутствующий `--spec`, недекодируемый `--plan` и неразбираемый `.py`;
    сообщение одной строкой, без traceback;
  - код `2` старше `1`: битый блок правил при грязном `src` → `2`;
  - гейт ничего не пишет: `git status --porcelain --ignored` до и после
    совпадает, каталог `.disputatio/` не создаётся;
  - **подключение**: `run_gate(GateSpec("wiring", f"{disp} gate wiring
    …"), repo)`, где `disp = Path(sys.executable).parent / "disp"` —
    entry point той же установки, что гоняет тесты, даёт `pass` на
    покрытом дереве и `fail` на непокрытом, `exit_code` не `None`, то
    есть не `skip`. Абсолютный путь делает тест независимым от `PATH` и
    сам служит примером обязательства §2.1. Отсутствие файла `disp` рядом
    с интерпретатором — провал теста, а не `skip`.
- [ ] **Шаг 2: реализация.** Подпарсер `gate` с обязательной
  подкомандой `wiring`; аргументы `--spec`, `--plan`, `--src` (по умолчанию
  `src`), `--root` (по умолчанию `.`), `set_defaults(journal=False)` —
  гейт не пишет журнал ошибок в чужую ленту. Обработчик: чтение `--spec` и
  `--plan` как UTF-8 (ошибка чтения/декодирования → `WiringInputError`) →
  `read_snapshot` → `dirty_src_paths` → `check_wiring` → печать
  `render_report` → `0`, если находок нет, иначе `1`. `WiringInputError`
  ловится в обработчике: сообщение в stdout сводкой и в stderr, код `2`.
  Прочие исключения не глушатся.
- [ ] **Шаг 3:** ruff, pyrefly, suite; коммит
  `feat(cli): disp gate wiring`.

---

### Задача 7: приёмка на историческом снимке

Реализует §7: обязательные приёмочные примеры.

**Файлы:**
- Create: `tests/fixtures/wiring/src-23c9297.tar.gz` — `git archive
  --format=tar.gz 23c929776192e82e7463dd204743dd484694bc8d src`
- Create: `tests/fixtures/wiring/README.md` — происхождение архива и
  команда пересборки
- Create: `tests/verifier/test_wiring_acceptance.py`

- [ ] **Шаг 1: фикстура.** Собрать архив командой выше. Тест-хелпер
  разворачивает его во временный каталог, делает `git init` и коммит.
- [ ] **Шаг 2: тест дословности.** Отпечаток развёрнутой фикстуры равен
  `70637371794b53ddc85dffb10624838e53ffb07c`. Это первый тест модуля: при
  его провале остальные результаты ничего не значат.
- [ ] **Шаг 3: таблица §7** — пять тестов, по строке на пример, с правилами
  из §3.1 и синтетическими планами. План для строки «оба правила» содержит
  разделы `### Задача 1:` и `### Задача 5:` со ссылками на все три места.
  Отдельно: на полном снимке нарушений ровно три — строки 581 и 1071
  (`count=1` у каждой) и строка 109 с `doc_sessions`.
- [ ] **Шаг 4:** ruff, pyrefly, suite; коммит
  `test(verifier): приёмка гейта wiring на снимке 23c9297`.

---

### Задача 8: документация

Реализует §9.

**Файлы:**
- Modify: `disputatio-SPEC-002-doc-pipeline.md` — в §6 после таблицы
  baseline один абзац: дополнительный гейт `wiring` для вида `pair`,
  ссылка на `docs/specs/2026-09-24-wiring-gate-design.md`, в baseline не
  входит.
- Modify: `docs/document-pipeline.md` — пример `[[pipeline.gates]]` из §2 и
  оба обязательства оператора из §2.1 (тот же `disp` в `PATH`; проверка
  статуса `wiring` в `verification.json` первого раунда: `skip` значит, что
  гейт не запускался).
- Modify: `TODO.md` — пункт `gate-declared-not-wired` закрыть со ссылкой на
  PR.

- [ ] **Шаг 1:** правки текста.
- [ ] **Шаг 2:** полный suite, ruff, pyrefly; коммит
  `docs: гейт wiring в SPEC-002 §6 и document-pipeline`.

---

## Самопроверка плана

**Покрытие спеки.** §1 граница и не-цели → глобальные ограничения (что не
редактируется), задачи 3–5 (ровно два правила); §2 вход и CLI → задача 6;
§2.1 `skip` и обязательства оператора → задача 6 (тест фактического
запуска), задача 8 (документация); §3.1 → задача 2; §3.2–§3.4 → задача 3;
§3.3 граница гарантии — отрицательных тестов не требует, кроме «чужой `C`
не нарушение» (задача 3); §3.5 → задача 4; §4 → задача 1 (отпечаток,
чистота, чтение из git) и задача 3 (разбор всех `.py`, ошибки разбора);
§5.1 → задача 2; §5.2–§5.3 → задачи 2 (разделы задач), 5 (находки); §6
коды и вывод → задачи 5, 6; §7 → задача 7 и сопутствующие случаи в
задачах 1, 3–6; §8 раскладка → задачи 1–6; §9 → задача 8.

**Решений сверх спеки план не вводит**, кроме трёх технических, которые
спека оставляет реализации: имя исключения `WiringInputError`, `--root` у
подкоманды (как у прочих подкоманд `disp`) и абсолютный путь к `disp`
рядом с `sys.executable` в тесте подключения, чтобы тест не зависел от
`PATH`.

**Промежуточные состояния.** Задачи 1–5 аддитивны: новые модули без
потребителей, suite зелёный после каждой. Подкоманда появляется в задаче 6
целиком. Документация — последней, когда гейт существует.

**Инвентарь меняемых интерфейсов.** Существующие интерфейсы не меняются.
Единственная правка существующего кода — `cli.py`: новый подпарсер и
обработчик. Потребитель `_build_parser` один — `main` (`cli.py:173`,
сверено `grep -rn "_build_parser" src tests`); перед задачей 6 сверить
повторно.
