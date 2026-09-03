"""Целостность control plane: политика P9 вокруг хода автора (SPEC-002 P9, §7.1).

Набор держит четыре утверждения, и каждое из них — про то, что ломается
тихо.

* **Снапшот пишется ровно в один файл — в анкер.** Дублирование его в
  манифест потребовало бы согласовать две файловые границы одной атомарной
  операцией, чего сделать нельзя: падение между записями оставило бы
  расхождение, неотличимое от подмены, и штатный крах читался бы как
  tampering. Поэтому `before_author_turn` манифест не трогает вовсе, и
  проверяется это байтами манифеста до и после хука.
* **Подмена манифеста ловится сверкой против анкера.** Это тот самый
  сценарий, ради которого анкер вынесен из рабочего дерева: манифест автору
  достижим, анкер — нет, а снапшота внутри манифеста не существует, так что
  подделывать нечего.
* **Журналы сверяются prefix-property, а не равенством.** Легальный append
  оркестратора обязан проходить, усечение — нет; иначе сверка либо роняла бы
  пайплайн на каждом штатном событии, либо не замечала бы вырезанной истории.
* **Идентификатор хода выводится из содержимого, а не из счётчика.** Повтор
  того же хода после краха обязан дать ту же строку (идемпотентность по
  `{kind, session_id, round, operation_id}`), а вторая попытка того же
  раунда — СВОЮ запись: иначе `turn_completed` первой попытки остался бы
  последней записью журнала, и подмена внутри второй попытки на resume не
  проверялась бы вовсе.

Диск здесь настоящий, а стенд — минимальный: политике нужны только файлы
control plane, и собирать вокруг них пайплайн целиком значило бы проверять
runner там, где проверяется сверка.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from disputatio.contracts import (
    SCHEMA_V2,
    AgentRef,
    BudgetUsed,
    Limits,
    Mode,
    Role,
    SessionPhase,
    SessionState,
    TaskSpec,
)
from disputatio.events import AnchorRecord, IntegrityAnchor
from disputatio.runtime.errors import ConfigError, ControlPlaneTampered
from disputatio.runtime.pipeline_integrity import (
    ControlPlane,
    PipelineIntegrityPolicy,
)

SLUG: Final = "pair-docs"
SESSION_ID: Final = "pair-r1"
MANIFEST_BYTES: Final = (
    b'{"schema": "disputatio/pipeline/v1", "pipeline_id": "pair-docs"}'
)


def _plane(workspace: Path) -> ControlPlane:
    """Control plane одной ревизии поверх подготовленного диска.

    Пути журналов подаёт тест: `runtime` их не вычисляет вовсе — путь
    `events.jsonl` там запрещён скан-правилом [DESIGN-016], потому что
    журнал открывает ровно один писатель. Здесь их роль играет знание
    раскладки, доступное набору.
    """
    pipeline_dir = workspace / ".disputatio" / "pipelines" / SLUG
    artifact_root = pipeline_dir / "sessions" / SESSION_ID
    return ControlPlane(
        workspace_root=workspace,
        pipeline_dir=pipeline_dir,
        artifact_root=artifact_root,
        append_only_paths=(
            pipeline_dir / "events.jsonl",
            artifact_root / ".disputatio" / "events.jsonl",
        ),
    )


def _seed(workspace: Path) -> ControlPlane:
    """Кладёт на диск полный набор файлов control plane одной ревизии."""
    plane = _plane(workspace)
    plane.pipeline_dir.mkdir(parents=True)
    (plane.pipeline_dir / "pipeline.json").write_bytes(MANIFEST_BYTES)
    (plane.pipeline_dir / "task.md").write_text("задача\n", encoding="utf-8")
    (plane.pipeline_dir / "config.toml").write_text("[pipeline]\n", encoding="utf-8")
    (plane.pipeline_dir / "checklists.toml").write_text("[spec]\n", encoding="utf-8")
    (plane.pipeline_dir / "events.jsonl").write_text(
        '{"type": "phase_change"}\n', encoding="utf-8"
    )

    session = plane.artifact_root / ".disputatio"
    (session / "rounds" / "001").mkdir(parents=True)
    (session / "session.json").write_text('{"state": "PROPOSING"}', encoding="utf-8")
    (session / "config.toml").write_text("[agents]\n", encoding="utf-8")
    (session / "events.jsonl").write_text(
        '{"type": "state_change"}\n', encoding="utf-8"
    )
    (session / "rounds" / "001" / "review.json").write_text(
        '{"verdict": "request_changes"}', encoding="utf-8"
    )
    return plane


def _policy(workspace: Path, anchor_root: Path) -> PipelineIntegrityPolicy:
    """Политика P9 поверх пустого анкера вне рабочего дерева."""
    anchor = IntegrityAnchor(anchor_root, workspace, SLUG)
    anchor.create_empty()
    return PipelineIntegrityPolicy(anchor=anchor, control_plane=_plane(workspace))


def _state(round_no: int = 1) -> SessionState:
    """Сессия, durable-но стоящая в `PROPOSING` раунда `round_no`."""
    return SessionState(
        schema=SCHEMA_V2,
        session_id=SESSION_ID,
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
        state=SessionPhase.PROPOSING,
        current_round=round_no,
        task=TaskSpec(prompt="задача", mode=Mode.DOCUMENT),
        agents={
            Role.AUTHOR: AgentRef(adapter="fake", model="m"),
            Role.REVIEWER: AgentRef(adapter="fake", model="m"),
        },
        limits=Limits(
            max_rounds=5,
            max_total_tokens=10_000,
            max_wall_seconds=600,
            schema_retries=2,
        ),
        budget_used=BudgetUsed(),
    )


def _records(anchor: IntegrityAnchor) -> list[AnchorRecord]:
    """Все записи журнала — читаются с диска, а не из памяти политики."""
    lines = anchor.path.read_text(encoding="utf-8").splitlines()
    return [AnchorRecord.model_validate(json.loads(line)) for line in lines if line]


def test_pre_turn_snapshot_goes_only_into_the_anchor(tmp_path: Path) -> None:
    """`before_author_turn` пишет снапшот в анкер и НЕ трогает манифест (N1)."""
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")
    manifest = plane.pipeline_dir / "pipeline.json"

    policy.before_author_turn(_state())

    assert manifest.read_bytes() == MANIFEST_BYTES
    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    records = _records(anchor)
    assert [record.kind for record in records] == ["pre_turn"]
    # Identity полная: §8.1 требует сверку ДО чтения манифеста, и брать
    # session_id/round оттуда было бы кругом.
    assert (records[0].session_id, records[0].round) == (SESSION_ID, 1)
    assert records[0].immutable, "снапшот неизменяемых файлов пуст"
    assert set(records[0].append_only) == {
        ".disputatio/pipelines/pair-docs/events.jsonl",
        ".disputatio/pipelines/pair-docs/sessions/pair-r1/.disputatio/events.jsonl",
    }


def test_manifest_substitution_is_caught_by_the_anchor(tmp_path: Path) -> None:
    """Подменённый автором манифест ловится сверкой — снапшота в нём нет.

    Сценарий, ради которого анкер вынесен из дерева: манифест автору
    достижим, анкер нет, и подделывать внутри манифеста нечего.
    """
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")
    policy.before_author_turn(_state())

    (plane.pipeline_dir / "pipeline.json").write_bytes(b'{"phase": "DONE"}')

    with pytest.raises(ControlPlaneTampered) as excinfo:
        policy.after_author_turn(_state())
    assert "pipeline.json" in str(excinfo.value)
    # Отметки о завершении хода нет: сверка не прошла, и следующий resume
    # обязан увидеть последней запись `pre_turn`.
    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    assert [record.kind for record in _records(anchor)] == ["pre_turn"]


def test_round_artifact_substitution_is_caught(tmp_path: Path) -> None:
    """Переписанный артефакт раунда — то же нарушение, что и подмена манифеста."""
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")
    policy.before_author_turn(_state())

    review = plane.artifact_root / ".disputatio" / "rounds" / "001" / "review.json"
    review.write_text('{"verdict": "approve"}', encoding="utf-8")

    with pytest.raises(ControlPlaneTampered) as excinfo:
        policy.after_author_turn(_state())
    assert "review.json" in str(excinfo.value)


def test_new_control_plane_file_is_caught(tmp_path: Path) -> None:
    """Дописанный автором артефакт раунда — тоже подмена, а не «просто файл».

    Сверка по равенству хешей ЗАПИСАННЫХ путей такой файл пропустила бы:
    его в снапшоте нет. Поэтому сравниваются и наборы путей.
    """
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")
    policy.before_author_turn(_state())

    forged = plane.artifact_root / ".disputatio" / "rounds" / "001" / "decision.json"
    forged.write_text('{"outcome": "converged"}', encoding="utf-8")

    with pytest.raises(ControlPlaneTampered) as excinfo:
        policy.after_author_turn(_state())
    assert "decision.json" in str(excinfo.value)


def test_event_log_truncation_is_caught(tmp_path: Path) -> None:
    """Усечение журнала — нарушение prefix-property, а не «журнал стал короче»."""
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")
    policy.before_author_turn(_state())

    (plane.artifact_root / ".disputatio" / "events.jsonl").write_text(
        "", encoding="utf-8"
    )

    with pytest.raises(ControlPlaneTampered) as excinfo:
        policy.after_author_turn(_state())
    # Диагноз, а не только факт отказа: усечение и переписанный префикс —
    # разные действия, и различает их только сравнение длин.
    assert "events.jsonl: журнал усечён" in str(excinfo.value)


def test_rewritten_log_prefix_is_caught(tmp_path: Path) -> None:
    """Переписанная старая часть журнала при той же длине — тоже нарушение."""
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")
    policy.before_author_turn(_state())

    log = plane.artifact_root / ".disputatio" / "events.jsonl"
    log.write_text('{"type": "state_chunge"}\n', encoding="utf-8")

    with pytest.raises(ControlPlaneTampered):
        policy.after_author_turn(_state())


def test_legal_append_passes_and_marks_the_turn_completed(tmp_path: Path) -> None:
    """Легальный append оркестратора проходит; успех отмечается в анкере.

    Без отметки завершения последняя запись успешного хода осталась бы
    `pre_turn`, а runtime сразу после сверки законно пишет артефакты раунда
    и двигает `session.json` — следующий resume прочитал бы это как подмену.
    """
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")
    policy.before_author_turn(_state())

    log = plane.artifact_root / ".disputatio" / "events.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "agent_text_delta"}\n')

    policy.after_author_turn(_state())

    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    records = _records(anchor)
    assert [record.kind for record in records] == ["pre_turn", "turn_completed"]
    assert records[1].operation_id == records[0].operation_id
    assert (records[1].session_id, records[1].round) == (SESSION_ID, 1)


def test_repeated_pre_turn_after_crash_is_one_record(tmp_path: Path) -> None:
    """Крах между append'ом и началом хода: повтор пишет ту же строку.

    Идемпотентность по `{kind, session_id, round, operation_id}`: запись о
    ходе, который не начался, сверку не ломает — она описывает состояние,
    которого никто не менял.
    """
    workspace = tmp_path / "repo"
    _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")

    policy.before_author_turn(_state())
    policy.before_author_turn(_state())

    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    records = _records(anchor)
    assert [record.kind for record in records] == ["pre_turn"]
    policy.after_author_turn(_state())


def test_second_attempt_of_the_same_round_gets_its_own_record(tmp_path: Path) -> None:
    """Вторая попытка того же раунда — своя запись, а не дубликат первой.

    Иначе последней записью журнала осталась бы `turn_completed` первой
    попытки, и подмену внутри второй попытки resume не проверял бы вовсе:
    сверка применяется, только если последняя запись — `pre_turn`.
    """
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")

    policy.before_author_turn(_state())
    policy.after_author_turn(_state())
    # Между попытками runtime законно двигает `session.json`
    # (`handle_schema_invalid` пишет счётчик повторов).
    (plane.artifact_root / ".disputatio" / "session.json").write_text(
        '{"state": "PROPOSING", "retries": 1}', encoding="utf-8"
    )
    policy.before_author_turn(_state())

    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    records = _records(anchor)
    assert [record.kind for record in records] == [
        "pre_turn",
        "turn_completed",
        "pre_turn",
    ]
    assert records[2].operation_id != records[0].operation_id
    policy.after_author_turn(_state())


def test_an_unchanged_control_plane_still_gets_a_second_record(
    tmp_path: Path,
) -> None:
    """Вторая попытка при БАЙТ-В-БАЙТ том же control plane — тоже своя запись.

    Идентификатор хода замешивает identity предыдущей записи журнала именно
    ради этого случая: не замешивай — попытка получила бы идентификатор
    первой, дедупликация отбросила бы её строку, и последней в журнале
    осталась бы `turn_completed`. Resume тогда пропустил бы сверку второй
    попытки: она применяется, только если последняя запись — `pre_turn`.
    """
    workspace = tmp_path / "repo"
    _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")

    policy.before_author_turn(_state())
    policy.after_author_turn(_state())
    policy.before_author_turn(_state())

    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    records = _records(anchor)
    assert [record.kind for record in records] == [
        "pre_turn",
        "turn_completed",
        "pre_turn",
    ]
    assert records[2].operation_id != records[0].operation_id


def test_policy_refuses_an_anchor_inside_the_workspace(tmp_path: Path) -> None:
    """`anchor_path` внутри рабочего дерева — отказ на конструировании политики.

    Регресс предусловия §3.1: анкер, лежащий в дереве автора, анкером не
    является, и заметить это обязан не только `run`.
    """
    workspace = tmp_path / "repo"
    _seed(workspace)
    inside = workspace / ".disputatio" / "anchors"
    anchor = IntegrityAnchor(inside, workspace, SLUG)
    anchor.create_empty()

    with pytest.raises(ConfigError):
        PipelineIntegrityPolicy(anchor=anchor, control_plane=_plane(workspace))


def test_snapshot_hashes_are_the_bytes_on_disk(tmp_path: Path) -> None:
    """Хеш неизменяемого файла — sha256 его байтов, а не перенормализации."""
    workspace = tmp_path / "repo"
    plane = _seed(workspace)
    snapshot = plane.snapshot(
        session_id=SESSION_ID, round_no=1, operation_id="turn-probe"
    )

    manifest_key = ".disputatio/pipelines/pair-docs/pipeline.json"
    assert (
        snapshot.immutable[manifest_key] == hashlib.sha256(MANIFEST_BYTES).hexdigest()
    )


def test_semantic_proof_is_part_of_immutable_surface(tmp_path: Path) -> None:
    """WS-65 BEH-01 (приёмка PR #90, круг 8): semantic_proof.json — durable
    control-plane артефакт того же окна, что и снапшоты, — входит в
    P9-поверхность: подмена меняет immutable-хеши, отсутствие (legacy)
    выражено членством и не ломает снятие снапшота."""
    plane = _seed(tmp_path)
    # legacy: файла нет — снапшот снимается, имени в наборе нет
    legacy = plane.immutable_hashes()
    assert "semantic_proof.json" not in {name.split("/")[-1] for name in legacy}

    proof_path = plane.pipeline_dir / "semantic_proof.json"
    proof_path.write_bytes(b'{"projection_schema_version": "1"}')
    with_proof = plane.immutable_hashes()
    (key,) = [k for k in with_proof if k.endswith("semantic_proof.json")]

    proof_path.write_bytes(b'{"projection_schema_version": "tampered"}')
    tampered = plane.immutable_hashes()
    assert tampered[key] != with_proof[key]
