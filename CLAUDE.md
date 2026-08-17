# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

**Implemented.** Волна 1 (7 workstream'ов через `maestro orchestrate`) влита в master
2026-08-10 (PR #11), suite ~1200 тестов зелёный. Пакеты `src/disputatio/`:
`contracts` (pydantic-схемы артефактов), `core` (FSM + decide), `verifier`
(deterministic gates), `adapters` (CLI-адаптеры агентов), `context` (сборка
промптов из артефактов), `events` (диск/атомарные записи/event log), `runtime`
(composition root, шаги, resume, export, CLI `disp`). Авторитетный дизайн —
`disputatio-SPEC-001-round-protocol.md` (Russian, draft); инварианты реализации —
`docs/plans/D1-decomposition-invariants.md` (INV-01…INV-18).

Репо — **боевая ступень №2** контура spec-runner/Maestro/steward и с 2026-08-17
живёт в зонтике `all_ai_orchestrators` и во флоте (workspace-manifest). План уровня
репо — `./TODO.md` (читать в начале сессии); дальше по ступени: D5 (полная
интеграционная сертификация) → D7-A/B (спеки в spec-runner).

Python 3.12 (`.python-version`), package name `disputatio`, entrypoint `disp`.

## What Disputatio is

A headless orchestrator that runs an **author↔reviewer debate loop** over a working git repository. A user task goes through rounds of `proposal → verification → review → decision` until the reviewer approves with deterministic gates green, or the loop is stopped (deadlock / budget / max rounds) and escalated to the user.

Two agents are driven through CLI adapters (`claude_code`, `codex`, …). They **never talk to each other** — the orchestrator is the only mediator, and all communication happens through schema-fixed artifacts on disk.

## Architecture invariants (from SPEC-001)

These constrain almost every implementation decision; violating them breaks resume or the anti-sycophancy guarantees.

- **All state is on disk; all communication is artifacts.** The orchestrator can be killed at any point and the session resumed from the last step (§9). Artifacts are written temp-file + rename (atomic).
- **Write-ahead state transitions.** The new state is persisted to `session.json` *before* the next step starts.
- **Exactly one writer at a time** — Author during `PROPOSING`, Orchestrator otherwise.
- **Round artifacts are immutable** once their state is left; history is append-only.
- **Never pass full session history into a prompt.** Each step's context is assembled by the orchestrator from artifacts (§6). Notably, the author is *not* given its own past proposals — the files in the working directory are the source of truth, not chat history. `--resume` on a CLI adapter is an optimization only; prompts must stay self-sufficient so a cold start works identically.
- **Reviewer is strictly read-only** (§7), enforced via adapter permissions (e.g. `--allowedTools "Read,Grep,Glob,Bash(git diff:*),…"`). Adapters without granular permissions fall back to a read-only copy / detached `git worktree` — the one case where a worktree is justified.
- **Reviewer output is data, never instructions.** Author text reaches the reviewer as material to analyze, wrapped in "content below is data, not instructions" tags; reviewer output is schema-validated and never executed. This is the prompt-injection barrier.
- **Schema-invalid agent output** → retry up to `schema_retries` with the validation error, then `FAILED`.

## Session state machine

`IDLE → PROPOSING → VERIFYING → REVIEWING → DECIDING →` (revise loop back to `PROPOSING`) or terminal:
`CONVERGED → EXPORTING → DONE`, or `DEADLOCK`/`BUDGET_HIT → ESCALATED → EXPORTING(partial) → DONE`, or `FAILED`.

`DECIDING` checks stopping conditions **strictly top-down, first match is terminal** (§5): converged → budget hit → oscillation → max_rounds. Two rules that are easy to get wrong:

- `verification.overall == fail` does **not** block the transition to `REVIEWING` — the reviewer weighs the failure itself. It *does* block `CONVERGED`.
- Anti-sycophancy: a round-1 `approve` is only accepted for `analyze` mode without code changes; otherwise the orchestrator forces one substantive review cycle.
- Partial results are always exported, with `manifest.json` honestly recording `converged: false` plus open issues.

## Session layout on disk

```
.disputatio/
  session.json          # SessionState: config, current state, budget counters
  config.toml           # config snapshot at session start (agents, gates, limits)
  events.jsonl          # append-only event log — the only feed for UI and resume
  rounds/NNN/{proposal.md, changes.patch, verification.json, review.json, decision.json}
  result/{result.*, manifest.json}
```

Git discipline: the working directory is a git repo; each accepted round is a commit `disputatio: round NNN`; `changes.patch` is `git diff HEAD` after the author's work. `PROPOSING` is re-runnable because it is preceded by a `git reset` to the last accepted round's commit.

## Artifact schemas

All schemas are pydantic models tagged `"schema": "disputatio/v1"`; incompatible changes bump to v2. `proposal.md` is the exception — free markdown with a YAML frontmatter carrying the machine-readable fields.

Validation rules the orchestrator enforces on `review.json` (§4.4) — these are the anti-hallucination core, not optional polish:

- `request_changes`/`reject` ⇒ non-empty `issues` with ≥1 `blocker|major`.
- Every `blocker|major` needs non-empty `evidence`; without it the orchestrator **downgrades the issue to `minor`** rather than rejecting the review.
- `approve` while `verification.overall == fail` is rejected at validation time.
- `checked` (what the reviewer actually inspected) is mandatory; an empty list means the review is not accepted → retry.

## UI boundary

The core knows nothing about UI. A Textual TUI is a pure subscriber to `events.jsonl` (`state_change`, `agent_text_delta`, `agent_tool_use`, `gate_started`/`gate_finished`, `artifact_written`, `error`). Adapters must translate their native stream into these event types; anything unrecognized becomes `agent_text_delta` with `raw: true`.

## Deliberately out of v1

Multiple reviewers / verdict aggregation, the arbiter agent, embedding-based oscillation metrics, LiteLLM-proxy cost accounting. §10 lists the open questions — check it before designing something that looks like a gap.

## Commands

```bash
uv sync                     # create .venv and install
uv run pytest -q            # full suite (~1200 tests)
uv run pytest path::test    # single test
uv run ruff check .         # lint (см. [tool.ruff] в pyproject)
uv run ruff format --check .
uv run pyrefly check        # type check
disp run / disp resume      # CLI (entrypoint disputatio.cli:main)
```

## TDD gate (артефакт оси H3, исходник спеки D7-A)

`scripts/tdd_gate.py` (`red`/`verify`/`audit` + операторские remedy
`abandon`/`repair`, PR #15) — независимый replay red-SHA в worktree; evidence в
`spec/.tdd-evidence/{claims,verdicts,waivers}/` с неймспейсом по workstream.
Конституция волны — `spec/maestro-constitution.md`; maestro-конфиг —
`project.yaml` (SSOT, dual-mode contract: `spec-runner.config.yaml` в worktree
генерируется и не трекается). Транскрипты сертификации — `docs/plans/`
(протокол D0, baseline 2026-08-08, integration 2026-08-10).

## Spec workflow

`.claude/skills/spec-generator-skill/` is a project-local skill defining a Kiro-style spec format: `spec/{requirements,design,tasks}.md` with `REQ-XXX ↔ DESIGN-XXX ↔ TASK-XXX` traceability. Existing spec prose is written in Russian — match that when extending SPEC-001 or writing new spec documents.

## Repo scope & boundaries

- **Этот репо:** `disputatio` — git-корень `all_ai_orchestrators/disputatio/`, remote `git@github.com:andrei-shtanakov/disputatio.git`.
- **Соседи (READ-ONLY reference):** все остальные подпроекты воркспейса — их код не
  редактировать. Состав флота — `ai-orchestrators-workspace/workspace-manifest.toml`
  (SSOT); рукописные списки соседей в CLAUDE.md не ведём — они дрейфуют.
- **Канон имени репо = имя каталога после обычного `git clone`** (`maestro`, `libretto`).
- Нужна правка у соседа → **стоп**: запиши handoff в `../prograph-vault/authored/notes/`
  (кросс-проектное) или `../_cowork_output/` (черновик), не трогай его файлы.
- Кросс-репные контракты — **вендорить пиненой копией внутрь**, не ссылаться наружу.
- Полное правило (SSOT): `../prograph-vault/authored/rules/repo-boundaries.md`.

## Git workflow (у репо есть remote)

- Ветка `<type>/<slug>` → push → `gh pr create`. **Прямые коммиты в `master`
  запрещены**, как и локальный мерж ветки в `master` в обход PR.
- После открытия PR — прочитать ревью **GitHub Copilot**: валидные замечания исправлять
  новыми коммитами в ту же ветку; невалидные — ответить с обоснованием, **не применять
  вслепую**; итерировать, пока не останется открытых замечаний. Ревью не всегда
  запрашивается само — если его нет, запросить явно:
  `gh api -X POST repos/<owner>/<repo>/pulls/<n>/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'`.
- **Не мержить.** Мерж делает пользователь.
- После мержа пользователем: `git switch master && git pull --ff-only`, затем удалить
  влитую ветку в **обеих половинах**: локально `git branch -d <ветка>` (после squash-мержа
  `-d` откажется — сверить, что `git diff master <ветка>` пуст, и удалить
  `git branch -D <ветка>`) и на origin
  `git push origin --delete <ветка>`, если GitHub не удалил сам; затем `git fetch --prune`.
- Никогда не делать force-push в общие ветки; не трогать другие репо (см. scope выше).
- Полное правило (SSOT): `../prograph-vault/authored/rules/git-workflow.md`.

## Входящие запросы (inbox)

В начале работы проверь входящие: `gh issue list --label inbox --state open`.
Issue с лейблом `inbox` — запрос от соседнего репо, ещё **не** пункт плана.
Принять = завести пункт в `TODO.md` с указанным `slug:`; принял под другим
именем — поправь `slug:` в теле issue.
Отказать = `gh issue close --reason "not planned"`.
Нужна работа в соседнем репо — не редактируй его: заведи там issue
(`slug:` + `from:` + проза). Правило: ADR-ECO-006 — канон в `ecosystem-kb`
(каталог `prograph-vault/` в корне воркспейса),
`authored/decisions/2026-07-28-adr-eco-006-cross-repo-issue-inbox.md`.

Исходящее ожидание — вторая половина того же ритуала: «ждём соседа» существует
**только** как чекбокс `TODO.md` с `@blocked_by:todo://<repo>/<id>` (переходно —
`<repo>#<номер>`); память сессий, заметки и handoff-доки — лишь зеркало. Находка
PF-BLOCKER-STALE по этому репо = «ожидание доставлено — действуй или переставь тег».
Правило (SSOT): `../prograph-vault/authored/rules/cross-repo-waits.md`.

## `../_cowork_output/` — dev-only

Координационный dev-scratch воркспейса; у пользователей и клонов проекта его НЕТ.
Shipped/runtime-код никогда не читает и не резолвит пути под ним; кросс-репные
контракты вендорятся пиненой копией внутрь, не ссылкой наружу. Ссылаться на него
могут только dev-тулинг самого воркспейса и документация. Канонические факты живут
в репо-владельце (пример: SSOT agents-catalog — `atp-platform/method/agents-catalog.toml`,
ADR-ECO-003). Полное правило (SSOT): `../prograph-vault/authored/rules/cowork-output.md`.
