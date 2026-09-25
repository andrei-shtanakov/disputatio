"""Периметр P9 — все ревизии манифеста, а не только текущая (#112, SPEC-002 §2 P9).

Runner читает соседние ревизии: бюджет пайплайна — по `session.json` всех
сессий, возврат из pair — по `review.json` припаркованной `pair-rN`. Снимок
одной текущей ревизии оставлял эти чтения без защиты. Здесь пинится:

- подмена `session.json`/`review.json` соседа за ход автора ловится;
- появление файла в ревизии периметра, чей каталог ещё не создан, — тоже;
- периметр берётся из записи `pre_turn`, а не из манифеста после хода;
- штатная запись runner между ходами входит в следующий снимок;
- `pre_turn` без периметра — несовместимость, а не подмена.
"""

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
from disputatio.events import IntegrityAnchor
from disputatio.runtime.errors import (
    ControlPlaneTampered,
    SnapshotPerimeterIncompatible,
)
from disputatio.runtime.pipeline_integrity import (
    ControlPlane,
    PipelineIntegrityPolicy,
)

SLUG: Final = "pair-docs"
CURRENT: Final = "spec-r2"
PARKED: Final = "pair-r1"
FINISHED: Final = "spec-r1"
NOT_YET_CREATED: Final = "pair-r2"
REVISIONS: Final = tuple(
    f"sessions/{session_id}"
    for session_id in (FINISHED, PARKED, CURRENT, NOT_YET_CREATED)
)


def _pipeline_dir(workspace: Path) -> Path:
    """`.disputatio/pipelines/<slug>` стенда."""
    return workspace / ".disputatio" / "pipelines" / SLUG


def _session_dir(workspace: Path, session_id: str) -> Path:
    """Каталог `.disputatio` одной ревизии."""
    return _pipeline_dir(workspace) / "sessions" / session_id / ".disputatio"


def _seed(workspace: Path) -> None:
    """Каталог пайплайна и три существующие ревизии; четвёртой ещё нет."""
    pipeline_dir = _pipeline_dir(workspace)
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "pipeline.json").write_text("{}", encoding="utf-8")
    for session_id in (FINISHED, PARKED, CURRENT):
        session = _session_dir(workspace, session_id)
        (session / "rounds" / "001").mkdir(parents=True)
        (session / "session.json").write_text(
            f'{{"session_id": "{session_id}", "tokens": 100}}', encoding="utf-8"
        )
        (session / "config.toml").write_text("[agents]\n", encoding="utf-8")
        (session / "rounds" / "001" / "review.json").write_text(
            '{"verdict": "request_changes"}', encoding="utf-8"
        )


def _policy(workspace: Path, anchor_root: Path) -> PipelineIntegrityPolicy:
    """Политика P9 с периметром из четырёх ревизий поверх пустого анкера."""
    anchor = IntegrityAnchor(anchor_root, workspace, SLUG)
    anchor.create_empty()
    plane = ControlPlane(
        workspace_root=workspace,
        pipeline_dir=_pipeline_dir(workspace),
        revisions=REVISIONS,
    )
    return PipelineIntegrityPolicy(anchor=anchor, control_plane=plane)


def _state(round_no: int = 1) -> SessionState:
    """Текущая ревизия в `PROPOSING` раунда `round_no`."""
    return SessionState(
        schema=SCHEMA_V2,
        session_id=CURRENT,
        created_at=datetime(2026, 9, 25, tzinfo=UTC),
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


def _turn(tmp_path: Path) -> tuple[Path, PipelineIntegrityPolicy]:
    """Стенд и политика, уже записавшая `pre_turn` хода текущей ревизии."""
    workspace = tmp_path / "repo"
    _seed(workspace)
    policy = _policy(workspace, tmp_path / "anchors")
    policy.before_author_turn(_state())
    return workspace, policy


def test_pre_turn_records_the_whole_perimeter(tmp_path: Path) -> None:
    """Запись `pre_turn` несёт периметр и хеши соседних ревизий (§4.2)."""
    workspace, _ = _turn(tmp_path)

    record = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG).last_record()

    assert record is not None
    assert record.revisions == list(REVISIONS)
    base = f".disputatio/pipelines/{SLUG}/sessions"
    assert f"{base}/{PARKED}/.disputatio/session.json" in record.immutable
    assert f"{base}/{FINISHED}/.disputatio/rounds/001/review.json" in record.immutable


def test_neighbour_session_json_tampering_is_caught(tmp_path: Path) -> None:
    """Уменьшенный расход соседа за ход автора — подмена (обход бюджета)."""
    workspace, policy = _turn(tmp_path)
    (_session_dir(workspace, PARKED) / "session.json").write_text(
        f'{{"session_id": "{PARKED}", "tokens": 0}}', encoding="utf-8"
    )

    with pytest.raises(ControlPlaneTampered, match=f"{PARKED}/.disputatio/session"):
        policy.after_author_turn(_state())


def test_neighbour_review_tampering_is_caught(tmp_path: Path) -> None:
    """Переписанное ревью припаркованной pair — подмена (§7.3 читает его)."""
    workspace, policy = _turn(tmp_path)
    review = _session_dir(workspace, PARKED) / "rounds" / "001" / "review.json"
    review.write_text('{"verdict": "approve"}', encoding="utf-8")

    with pytest.raises(ControlPlaneTampered, match="review.json: содержимое"):
        policy.after_author_turn(_state())


def test_file_appearing_in_a_not_yet_created_revision_is_caught(
    tmp_path: Path,
) -> None:
    """Ревизия периметра без каталога остаётся в периметре: появление — нарушение."""
    workspace, policy = _turn(tmp_path)
    planted = _session_dir(workspace, NOT_YET_CREATED) / "session.json"
    planted.parent.mkdir(parents=True)
    planted.write_text("{}", encoding="utf-8")

    with pytest.raises(ControlPlaneTampered, match="появился за ход автора"):
        policy.after_author_turn(_state())


def test_removed_neighbour_file_is_caught(tmp_path: Path) -> None:
    """Исчезновение файла соседа проверяется тем же правилом."""
    workspace, policy = _turn(tmp_path)
    (_session_dir(workspace, FINISHED) / "config.toml").unlink()

    with pytest.raises(ControlPlaneTampered, match="исчез за ход автора"):
        policy.after_author_turn(_state())


def test_perimeter_comes_from_the_record_not_from_the_plane(tmp_path: Path) -> None:
    """Сверка обходит периметр ЗАПИСИ: узкая плоскость его не сужает.

    Моделирует сверку, чей собственный список ревизий после хода оказался
    бы другим (например, выведенным из переписанного манифеста): периметр
    обязан прийти из `pre_turn`, записанного до хода.
    """
    workspace, _ = _turn(tmp_path)
    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    record = anchor.last_record()
    assert record is not None
    (_session_dir(workspace, PARKED) / "session.json").write_text(
        "{}", encoding="utf-8"
    )
    narrow = ControlPlane(
        workspace_root=workspace,
        pipeline_dir=_pipeline_dir(workspace),
        revisions=(f"sessions/{CURRENT}",),
    )

    problems = narrow.for_record(record).violations(record)

    # Ровно одна находка — изменённый файл соседа. Плоскость, сверившая по
    # собственному узкому списку, дала бы вместо неё «исчез» на каждом файле
    # соседей: они есть в записи, но не в её обходе.
    session = f".disputatio/pipelines/{SLUG}/sessions/{PARKED}/.disputatio"
    assert len(problems) == 1
    assert problems[0].startswith(f"{session}/session.json: содержимое изменилось")


def test_runner_writes_between_turns_enter_the_next_snapshot(tmp_path: Path) -> None:
    """Штатные записи runner между ходами — не подмена: следующий снимок свежий."""
    workspace, policy = _turn(tmp_path)
    policy.after_author_turn(_state())
    # Между ходами runner законно пишет: решение раунда текущей ревизии,
    # итог соседней, манифест.
    (_session_dir(workspace, CURRENT) / "rounds" / "001" / "decision.json").write_text(
        '{"outcome": "continue"}', encoding="utf-8"
    )
    (_session_dir(workspace, PARKED) / "session.json").write_text(
        f'{{"session_id": "{PARKED}", "tokens": 150}}', encoding="utf-8"
    )
    (_pipeline_dir(workspace) / "pipeline.json").write_text(
        '{"phase": "SPEC_LOOP"}', encoding="utf-8"
    )

    policy.before_author_turn(_state(round_no=2))
    policy.after_author_turn(_state(round_no=2))

    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    last = anchor.last_record()
    assert last is not None
    assert (last.kind, last.round) == ("turn_completed", 2)


def test_pre_turn_without_perimeter_is_incompatible_not_tampered(
    tmp_path: Path,
) -> None:
    """Старый `pre_turn` без поля — несовместимость, а не пустой периметр."""
    workspace, _ = _turn(tmp_path)
    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    record = anchor.last_record()
    assert record is not None
    legacy = record.model_copy(update={"revisions": None})
    plane = ControlPlane(
        workspace_root=workspace, pipeline_dir=_pipeline_dir(workspace), revisions=()
    )

    with pytest.raises(SnapshotPerimeterIncompatible, match="узким периметром"):
        plane.for_record(legacy)


def test_only_pre_turn_lines_carry_the_perimeter(tmp_path: Path) -> None:
    """В журнале поле `revisions` есть у `pre_turn` и отсутствует у прочих (§4.2)."""
    workspace, policy = _turn(tmp_path)
    policy.after_author_turn(_state())

    anchor = IntegrityAnchor(tmp_path / "anchors", workspace, SLUG)
    lines = [
        json.loads(line)
        for line in anchor.path.read_text(encoding="utf-8").splitlines()
    ]

    assert [(line["kind"], "revisions" in line) for line in lines] == [
        ("pre_turn", True),
        ("turn_completed", False),
    ]
