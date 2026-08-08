# D1: инварианты декомпозиции SPEC-001 — оракул для steward compile-and-diff (D6)

> Статус: контракт-оракул. Зафиксирован ДО шага D6 «steward compile-and-diff»
> ступени пилота: при сравнении рукописного `project.yaml` с компилятом
> каноном являются ЭТИ инварианты, а не любой из двух YAML-файлов.
> Рукописный `project.yaml` — baseline-кандидат, не абсолютная истина.
> Ступень: `_cowork_output/plans/2026-08-08-disputatio-battle-stage.md` (зонтик).
> Редакция: 1 (2026-08-08).

## Структура волн

```
w-contracts                      ← волна 0 (единственный корень)
  ├── w-fsm        ┐
  ├── w-verifier   │
  ├── w-adapters   ├─ волна 1 (параллельно, рёбер между собой нет)
  ├── w-context    │
  └── w-events     ┘
        └── w-runtime            ← интеграция (после merge волны 1)
```

## Инварианты (проверяемые)

### Состав и DAG

- **INV-01 (IDs).** Множество workstream-ов ровно:
  `{w-contracts, w-fsm, w-verifier, w-adapters, w-context, w-events, w-runtime}`.
- **INV-02 (рёбра).** Зависимости ровно такие: каждый из
  `{w-fsm, w-verifier, w-adapters, w-context, w-events}` имеет
  `depends_on: [w-contracts]` и ничего больше; `w-runtime` имеет
  `depends_on` = все пять WS волны 1. Других рёбер нет.
- **INV-03 (один корень).** Ровно один WS без зависимостей: `w-contracts`.
- **INV-04 (параллельность волны 1).** Пять WS волны 1 не имеют рёбер друг к
  другу — все пять могут исполняться одновременно (это ось H2 ступени).

### Скоупы

- **INV-05 (дизъюнктность).** Скоупы попарно дизъюнктны — ни один путь не
  матчится глобами двух WS:

| WS | scope |
|---|---|
| w-contracts | `src/disputatio/contracts/**`, `tests/contracts/**` |
| w-fsm | `src/disputatio/core/**`, `tests/core/**` |
| w-verifier | `src/disputatio/verifier/**`, `tests/verifier/**` |
| w-adapters | `src/disputatio/adapters/**`, `tests/adapters/**` |
| w-context | `src/disputatio/context/**`, `tests/context/**` |
| w-events | `src/disputatio/events/**`, `tests/events/**` |
| w-runtime | `src/disputatio/runtime/**`, `src/disputatio/cli.py`, `tests/runtime/**`, `tests/cli/**`, `pyproject.toml`, `src/disputatio/__init__.py`, `tests/conftest.py` |

- **INV-06 (integration-owned files).** Ни один scope волн 0–1 не включает
  файлы, которыми владеет интеграция: `src/disputatio/__init__.py`,
  `pyproject.toml`, `tests/conftest.py`, `src/disputatio/cli.py`.
  WS волны 1, которому нужна общая fixture, кладёт её в СВОЙ
  `tests/<area>/conftest.py` внутри собственного scope.

### Границы ответственности

- **INV-07 (покрытие SPEC-001).** Каждый раздел спеки принадлежит ровно
  одному WS или явно отложен:

| § SPEC-001 | Владелец |
|---|---|
| §1 (термины) | справочный, без владельца |
| §2, §5 (FSM, DECIDING top-down, stopping rules) | w-fsm |
| §3 (файловая структура, atomic writes) | w-events |
| §4.1, §4.2, §4.4, §4.5 (модели + кросс-артефактная валидация) | w-contracts |
| §4.3 (VerificationReport-модель) | w-contracts (модель) / w-verifier (исполнение gates) |
| §6 (контекст-пассинг, гигиена ввода) | w-context |
| §7 (права инструментов, enforcement ролей) | w-adapters |
| §8 (события: схема) | w-contracts (модель) / w-events (запись) / w-adapters (трансляция нативного потока) |
| §9 (resume) | w-runtime |
| §10 (открытые вопросы) | вне волны, сознательно |

- **INV-08 (w-events без runtime-логики).** w-events реализует atomic
  artifact store и append-only event writer; НЕ реализует resume и run-loop.
- **INV-09 (w-adapters без verification).** w-adapters не исполняет
  verification gates — это w-verifier.
- **INV-10 (w-verifier без агентов).** w-verifier не вызывает агентские CLI;
  только детерминированные команды gates. **v1-контракт mutation-freedom**:
  `git status --porcelain` идентичен до и после прогона gates
  (tracked-состояние дерева не мутируется; ignored-кэши допустимы).
- **INV-11 (единственная точка композиции).** Protocol-интерфейсы (ports:
  `StateStore`, `EventSink`, `AgentAdapter`, `Verifier`) живут в w-contracts;
  волна 1 реализует их и тестируется на фейках; w-runtime — ЕДИНСТВЕННОЕ
  место, где ports связываются с реализациями (composition root).

### Инфраструктура исполнения

- **INV-12 (base branch).** `base_branch == pilot/wave-1`; Maestro-PRы
  workstream-ов целятся в неё; финальный PR `pilot/wave-1 → master` делает
  человек (шаг D5). Master инструментам запрещён (правило kapelle).
- **INV-13 (workspace).** `workspace_base` — сиблинг-каталог без точки и вне
  ignore-путей: `~/labs/disputatio-ws` (ловушка pyrefly: внутри
  dot-каталога тип-чек молча проверяет ноль файлов).
- **INV-14 (delegation).** Leaf-исполнение — spec-runner через Maestro
  (`spec_prefix: maestro-`); `test_command: uv run pytest -q`;
  `lint_command: uv run ruff check .`. Dual-mode contract соблюдён:
  `project.yaml` tracked (SSOT), `spec-runner.config.yaml` untracked и в
  `.gitignore`, Maestro регенерирует его в worktree.
- **INV-15 (кандидатность).** «Единственный корень w-contracts» — свойство
  ЭТОЙ decomposition, не вечный инвариант продукта.

## Секвенирование до D3 (порядок веток)

1. **D1 PR** (этот) → human merge в `master`.
2. **TDD-gate PR** (`scripts/tdd_gate.py` + `spec/plugins/tdd-gate/` +
   harness-конфиг) → human merge в `master`. До D3 в нём ОБЯЗАНА быть
   разрешена развилка enforcement: либо replay-verifier встроен в pre-commit
   `test_command`, либо пилот честно именуется «post-commit TDD audit».
   Одного `blocking: true` у post_done недостаточно (плагин исполняется
   после auto-commit/merge — установлено по коду spec-runner в D0-дизайне).
3. Создание `pilot/wave-1` от НОВОГО master (после обоих merge — иначе
   worktrees не увидят гейт).
4. **Повторный D2 preflight** (`maestro validate project.yaml --strict`) —
   tracked tree и harness-пути изменились.
5. D3 `maestro orchestrate`.

## Гранулярность leaf-задач (решение открытого вопроса дизайна)

3–6 leaf-задач на WS; каждая задача — один TDD-цикл (red→green→refactor).
Формулировки в `description` каждого WS требуют от декомпозиции задач
детерминированной приёмки (`uv run pytest -q` по области WS). Для оси H3
достаточно ≥1 leaf с полной evidence-цепочкой claim→verdict; кандидат —
задачи w-verifier (детерминированный контракт, mutation-freedom
проверяется тестом).
