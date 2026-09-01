"""Сборка, adoption и resume под вид: SPEC-002 v0.2 §2 P0/P10, §3.1, §8.1.

Три утверждения, и все три — про то, чего в документном пайплайне НЕ
происходит. Такие свойства нельзя проверять поведением: реализация, которая
«просто не срабатывает», прошла бы поведенческий тест и осталась бы вторым
видом, спрятанным внутри первого. Поэтому здесь проверяются объекты
(`isinstance` маршрутизатора), момент отказа (снимок мутируемых поверхностей
до и после) и приоритет отказов (подмена важнее чужого вида).
"""

from pathlib import Path
from typing import Any

import pytest

from disputatio.contracts import PipelineKind, PipelinePhase, TransitionReason
from disputatio.runtime import composition
from disputatio.runtime.composition import build_pipeline
from disputatio.runtime.config import AgentConfig, LimitsConfig
from disputatio.runtime.errors import AdoptionScopeError, ConfigError
from disputatio.runtime.git import GitCli
from disputatio.runtime.layout import round_dir
from disputatio.runtime.pipeline_adopt import (
    PairAdoptionRouter,
    SingleContourAdoptionRouter,
)
from disputatio.runtime.pipeline_config import SessionProfile
from disputatio.runtime.pipeline_integrity import ControlPlane

from ._pipeline_stand import (
    DOCUMENT_PATH,
    SLUG,
    Script,
    Stand,
    build_stand,
    git,
)

_ADAPTER: str = "fake"


def _profile() -> SessionProfile:
    return SessionProfile(
        author=AgentConfig(adapter=_ADAPTER, model="m"),
        reviewer=AgentConfig(adapter=_ADAPTER, model="m"),
        limits=LimitsConfig(
            max_rounds=3, max_total_tokens=1000, max_wall_seconds=60, schema_retries=2
        ),
    )


def _register_fake_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Реестр адаптеров — единственный подменённый шов сборки."""

    class _Agent:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def run(self, prompt: str, *, session_ref: str | None = None) -> Any:
            raise AssertionError("сборка не должна звать агента")

    monkeypatch.setitem(composition.ADAPTER_FACTORIES, _ADAPTER, _Agent)


def _deps(stand: Stand, monkeypatch: pytest.MonkeyPatch) -> Any:
    _register_fake_adapter(monkeypatch)
    return build_pipeline(
        stand.config,
        _profile(),
        stand.workspace,
        SLUG,
        git=GitCli(stand.workspace),
    )


def _live_document(tmp_path: Path, **kwargs: Any) -> Stand:
    """Документный стенд с оборванной doc-сессией — есть что возобновлять."""
    scripts = {
        "doc-r1": Script(outcome="park", raise_after_write=True),
        "doc-r2": Script(outcome="deadlock"),
    }
    stand = build_stand(tmp_path, scripts, kind=PipelineKind.DOCUMENT, **kwargs)
    stand.start()
    return stand


# --- P10: маршрутизатор adoption выбирается при сборке (§3.1) ---------


def test_document_kind_builds_single_contour_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """У документного вида СВОЯ реализация порта, а не общая с флагом."""
    stand = build_stand(tmp_path, {}, kind=PipelineKind.DOCUMENT)
    deps = _deps(stand, monkeypatch)

    assert isinstance(deps.intents.router, SingleContourAdoptionRouter)


def test_pair_kind_builds_pair_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand = build_stand(tmp_path, {})
    deps = _deps(stand, monkeypatch)

    assert isinstance(deps.intents.router, PairAdoptionRouter)


def test_document_pipeline_builds_no_boundary_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тот же P10 на втором шве: таблица политик документного вида пуста."""
    stand = build_stand(tmp_path, {}, kind=PipelineKind.DOCUMENT)
    deps = _deps(stand, monkeypatch)

    assert dict(deps.runner.boundary_policies) == {}


# --- P0: вид неизменяем, и отказ идёт ДО мутаций (§2, §8.1) -----------


def test_resume_with_config_of_other_kind_mutates_nothing(tmp_path: Path) -> None:
    """P0: отказ **до любой мутации**, а не просто отказ.

    `pytest.raises` доказывает только исключение. Норматив (§2 P0, §10)
    сильнее: реализация, успевшая переиграть раунд, дописать журнал или
    сдвинуть HEAD и лишь потом заметившая чужой вид, прошла бы такой тест
    зелёной. Поэтому сверяются наблюдаемые поверхности, которые resume
    вправе менять.
    """
    stand = _live_document(tmp_path)
    before = stand.mutable_surfaces()

    with pytest.raises(ConfigError, match="вид"):
        stand.resume_with(stand.config_of_kind(PipelineKind.PAIR)).resume(SLUG)

    assert stand.mutable_surfaces() == before


def test_tampered_plane_outranks_kind_check(tmp_path: Path) -> None:
    """Приоритет шага 0: подмена важнее чужого вида (§2 P0, §8.1).

    При повреждённом control plane и конфиге другого вида отказ обязан быть
    ПО ПОДМЕНЕ, а перевод в `FAILED` — законной мутацией. Без этого теста
    предыдущий требовал бы неизменности поверхностей и там, где спека её не
    обещает.
    """
    from disputatio.runtime.errors import ControlPlaneTampered

    stand = _live_document(tmp_path)
    plane = ControlPlane(
        workspace_root=stand.workspace,
        pipeline_dir=stand.pipeline_dir(),
        artifact_root=stand.artifact_root("doc-r1"),
        append_only_paths=(
            stand.pipeline_dir() / "events.jsonl",
            stand.artifact_root("doc-r1") / ".disputatio" / "events.jsonl",
        ),
    )
    stand.anchor().append_pre_turn(
        plane.snapshot(session_id="doc-r1", round_no=1, operation_id="turn-seeded")
    )
    review = round_dir(stand.artifact_root("doc-r1"), 1) / "review.json"
    review.write_text('{"verdict": "approve"}', encoding="utf-8")

    with pytest.raises(ControlPlaneTampered):
        stand.resume_with(stand.config_of_kind(PipelineKind.PAIR)).resume(SLUG)

    assert stand.manifest().phase is PipelinePhase.FAILED


def test_resume_of_own_kind_proceeds(tmp_path: Path) -> None:
    """Регрессия: своим конфигом документный пайплайн продолжается как обычно."""
    stand = _live_document(tmp_path)
    state = stand.rebuild().resume.resume(SLUG)

    assert state.kind is PipelineKind.DOCUMENT


# --- adoption вида document (§3.1) ------------------------------------


def test_adoption_outside_document_is_rejected_entirely(tmp_path: Path) -> None:
    stand = _live_document(tmp_path)
    (stand.workspace / "README.md").write_text("постороннее", encoding="utf-8")

    with pytest.raises(AdoptionScopeError):
        stand.resume.resume(SLUG, decision="adopt_external")


def test_adoption_of_document_opens_next_revision_without_transition(
    tmp_path: Path,
) -> None:
    """Правка документа → новая `doc`-ревизия внутри той же фазы (§3.1)."""
    stand = _live_document(tmp_path)
    (stand.workspace / DOCUMENT_PATH).write_text(
        "# чартер\n\nправка руками\n", encoding="utf-8"
    )

    state = stand.resume.resume(SLUG, decision="adopt_external")

    assert state.doc_sessions[-1].session_id == "doc-r2"
    assert all(t.to is not PipelinePhase.SPEC_LOOP for t in state.transitions)
    assert TransitionReason.EXTERNAL_SPEC_ADOPT not in [
        transition.reason for transition in state.transitions
    ]
    assert [decision.kind for decision in state.operator_decisions] == [
        "adopt_external"
    ]


def test_adopted_document_survives_the_checkpoint(tmp_path: Path) -> None:
    """Принятая правка durable в git: чекпоинт оператора её несёт (§3.1)."""
    stand = _live_document(tmp_path)
    (stand.workspace / DOCUMENT_PATH).write_text(
        "# чартер\n\nправка руками\n", encoding="utf-8"
    )

    stand.resume.resume(SLUG, decision="adopt_external")

    committed = git(stand.workspace, "show", f"HEAD:{DOCUMENT_PATH}")
    assert "правка руками" in committed
