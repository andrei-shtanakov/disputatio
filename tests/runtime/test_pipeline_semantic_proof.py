"""Доказательство immutable-проекции `[pipeline]` — BEH-01/12/13/15 (WS-disputatio-65).

Стенд — `_pipeline_stand.build_stand`: настоящий git, настоящий манифест,
фейковые драйвер/фабрика/экспортёр (см. её докстринг). Пайплайну вида
`document` дают штатно сойтись (`doc-r1` со сценарием по умолчанию), после
чего `task.md`/`config.toml`/`checklists.toml`/`semantic_proof.json` уже
лежат на диске и больше не меняются до конца жизни пайплайна — тампер тестов
приходит уже ПОСЛЕ этого штатного `run`, как и любая порча в проде приходила
бы уже после него.

`load_semantic_proof` вызывается здесь напрямую, а не через
`PipelineResume.resume`: встраивание в порядок §8.1 (P9 → манифест →
semantic proof → …) — предмет TASK-004 той же очереди задач; этот набор
проверяет функцию как самостоятельный, полностью тестируемый шаг (BEH-01,
BEH-12, BEH-13, BEH-15).
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from disputatio.contracts import PipelineKind, PipelineState
from disputatio.runtime.errors import UnprovableSemantics
from disputatio.runtime.pipeline_runner import PipelineRunner, SessionCreation
from disputatio.runtime.pipeline_semantic_proof import (
    PROJECTION_SCHEMA_VERSION,
    SEMANTIC_PROOF_NAME,
    build_projection,
    load_semantic_proof,
)

from ._pipeline_stand import PLAN_PATH, SLUG, SPEC_PATH, Script, Stand, build_stand


def _doc_stand(tmp_path: Path) -> Stand:
    """Пайплайн вида `document`, сошедшийся штатно за один раунд."""
    stand = build_stand(tmp_path, {"doc-r1": Script()}, kind=PipelineKind.DOCUMENT)
    stand.start()
    return stand


def _proof_bytes(stand: Stand, state: PipelineState) -> bytes:
    ref = state.semantic_proof
    assert ref is not None
    return (stand.pipeline_dir() / ref.path).read_bytes()


def _proof_json(stand: Stand, state: PipelineState) -> dict[str, Any]:
    return json.loads(_proof_bytes(stand, state))


def _canonical(payload: Mapping[str, Any]) -> bytes:
    """Та же сериализация, что и у `write_semantic_proof` — для подделки байтов."""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _with_proof_sha256(state: PipelineState, sha256: str) -> PipelineState:
    """`state` с подменённым `semantic_proof.sha256` — вход digest-негодных случаев."""
    ref = state.semantic_proof
    assert ref is not None
    return state.model_copy(
        update={"semantic_proof": ref.model_copy(update={"sha256": sha256})}
    )


def _rewrite_proof_with_matching_digest(
    stand: Stand, state: PipelineState, payload: Mapping[str, Any]
) -> PipelineState:
    """Пишет подделанный (но самосогласованный по digest) proof; сдвигает манифест.

    Имитирует «доказательство разбирается и digest сходится, но структурно
    или семантически негодно» — то есть все причины BEH-12, КРОМЕ
    `digest_mismatch` и «файл отсутствует»: тест обязан пройти проверку
    digest, чтобы дойти до проверяемой причины.
    """
    data = _canonical(payload)
    (stand.pipeline_dir() / SEMANTIC_PROOF_NAME).write_bytes(data)
    assert state.semantic_proof is not None
    new_ref = state.semantic_proof.model_copy(
        update={"sha256": hashlib.sha256(data).hexdigest()}
    )
    return state.model_copy(update={"semantic_proof": new_ref})


class _CrashBeforeFirstSession(RuntimeError):
    """Инжектированный крах между commit point первого intent'а и сессией."""


def _fixed_clock() -> Any:
    moment = datetime(2026, 9, 3, tzinfo=UTC)
    return lambda: moment


# ---------------------------------------------------------------------------
# BEH-01 — run атомарно фиксирует версионированную immutable-модель
# ---------------------------------------------------------------------------


def test_run_commits_versioned_proof_atomically(tmp_path: Path) -> None:
    """`run` до первой сессии фиксирует `semantic_proof` В ТОЙ ЖЕ атомарной
    записи манифеста, что и первый intent (BEH-01, FR-01): доказательство
    несёт версию канонизации и полную immutable-проекцию `[pipeline]`, а крах
    между этой записью и первой сессией (здесь — фабрика, которая никогда
    успешно не отрабатывает) не мешает — proof уже на диске.
    """
    stand = build_stand(tmp_path, {}, kind=PipelineKind.PAIR)

    def _crashing_factory(creation: SessionCreation) -> None:
        raise _CrashBeforeFirstSession(
            "процесс убит до первой сессии — proof обязан быть на диске"
        )

    def _unreachable_driver(
        artifact_root: Path, session_id: str, policy: object
    ) -> None:
        raise AssertionError("драйвер не вызывается раньше первой сессии")

    def _unreachable_exporter(*args: object, **kwargs: object) -> None:
        raise AssertionError("экспортёр не вызывается раньше первой сессии")

    runner = PipelineRunner(
        boundary_policies=stand.boundary_policies,
        store=stand.store,
        sink=stand.sink,
        git=stand.git,
        session_driver=_unreachable_driver,  # type: ignore[arg-type]
        session_factory=_crashing_factory,  # type: ignore[arg-type]
        exporter=_unreachable_exporter,  # type: ignore[arg-type]
        now=_fixed_clock(),
        config=stand.config,
        workspace_root=stand.workspace,
    )

    try:
        runner.run(SLUG, "ЗАДАЧА: перепроверить пару")
    except _CrashBeforeFirstSession:
        pass

    state = stand.manifest()
    ref = state.semantic_proof
    assert ref is not None, (
        "манифест не несёт ссылки на доказательство immutable-проекции — "
        "`run` обязан зафиксировать её атомарно, до первой сессии, одной "
        "записью с первым intent'ом"
    )
    assert ref.path == SEMANTIC_PROOF_NAME

    proof_bytes = (stand.pipeline_dir() / ref.path).read_bytes()
    assert hashlib.sha256(proof_bytes).hexdigest() == ref.sha256

    proof = json.loads(proof_bytes)
    assert proof["projection_schema_version"] == PROJECTION_SCHEMA_VERSION
    assert proof["pipeline_id"] == SLUG
    assert proof["sources"]["config"] == {
        "path": state.config.path,
        "sha256": state.config.sha256,
    }
    assert proof["sources"]["checklists"] == {
        "path": state.checklists.path,
        "sha256": state.checklists.sha256,
    }
    # Проекция несёт ровно то, что построил бы `build_projection` из живого
    # конфига стенда — тот же приём, каким `write_semantic_proof` собрала её
    # при `run` (FR-01: «описывать итоговые разобранные значения»).
    assert proof["projection"] == build_projection(stand.config)
    assert proof["projection"]["spec_path"] == SPEC_PATH
    assert proof["projection"]["plan_path"] == PLAN_PATH


# ---------------------------------------------------------------------------
# BEH-12 — недоказуемая семантика запрещает продолжение без fallback
# ---------------------------------------------------------------------------


def test_unprovable_semantics_fail_closed_without_fallback(tmp_path: Path) -> None:
    """Каждая из пяти причин недоказуемости отказывает без fallback и без
    автоматической записи/починки proof (BEH-12, FR-11): живой конфиг стенда
    (`stand.config`) никак не участвует в восстановлении — функция либо
    отдаёт проверенный проекцию, либо падает, и падение не трогает диск.
    """
    stand = _doc_stand(tmp_path)
    state = stand.manifest()
    pipeline_dir = stand.pipeline_dir()

    # Baseline: нетронутое доказательство восстанавливается.
    proof = load_semantic_proof(pipeline_dir, state)
    assert proof["projection_schema_version"] == PROJECTION_SCHEMA_VERSION

    before = _proof_bytes(stand, state)

    # 1. Отсутствует: манифест не несёт ссылки вовсе.
    missing_ref_state = state.model_copy(update={"semantic_proof": None})
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, missing_ref_state)
    assert excinfo.value.reason == "missing"

    # 2. Повреждён/неподтверждён: байты файла не сходятся с зафиксированным digest.
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(before + b"\ntampered")
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, state)
    assert excinfo.value.reason == "digest_mismatch"
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(before)

    # 3. Не разбирается: валидный digest, невалидный JSON.
    garbage = b"{not-json"
    garbage_state = _with_proof_sha256(state, hashlib.sha256(garbage).hexdigest())
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(garbage)
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, garbage_state)
    assert excinfo.value.reason == "parse_error"
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(before)

    # 4. Неподдерживаемая версия канонизации.
    unsupported = {**_proof_json(stand, state), "projection_schema_version": "99"}
    unsupported_state = _rewrite_proof_with_matching_digest(stand, state, unsupported)
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, unsupported_state)
    assert excinfo.value.reason == "unsupported_version"
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(before)

    # 5. Внутренне противоречив: proof называет чужой pipeline_id.
    contradictory = {**_proof_json(stand, state), "pipeline_id": "another-slug"}
    contradictory_state = _rewrite_proof_with_matching_digest(
        stand, state, contradictory
    )
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, contradictory_state)
    assert excinfo.value.reason == "contradiction"
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(before)

    # Ни один из пяти отказов не переписал и не «починил» файл на диске —
    # он неизменно возвращается к байтам штатного `run`, и после последнего
    # восстановления доказательство снова читается штатно.
    assert _proof_bytes(stand, state) == before
    assert load_semantic_proof(pipeline_dir, state) == proof


# ---------------------------------------------------------------------------
# BEH-13 — каждый источник доказательства проверяется и согласуется
# ---------------------------------------------------------------------------


def test_all_proof_sources_require_integrity_and_consistency(tmp_path: Path) -> None:
    """`config.toml` и `checklists.toml` проверяются НЕЗАВИСИМО (BEH-13, FR-12):
    порча одного отказывает даже когда другой (и манифест, и живой конфиг
    стенда) полностью в порядке — совпадение с одним источником не
    компенсирует порчу другого.
    """
    stand = _doc_stand(tmp_path)
    state = stand.manifest()
    pipeline_dir = stand.pipeline_dir()

    config_path = pipeline_dir / state.config.path
    checklists_path = pipeline_dir / state.checklists.path
    original_config = config_path.read_bytes()
    original_checklists = checklists_path.read_bytes()
    original_proof = _proof_bytes(stand, state)

    # Порча config.toml на диске: checklists.toml и сам proof-файл в порядке.
    config_path.write_bytes(original_config + b"\n# external edit\n")
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, state)
    assert excinfo.value.reason == "digest_mismatch"
    assert excinfo.value.artifact == state.config.path
    config_path.write_bytes(original_config)

    # Исчезновение config.toml — тоже отказ, а не «источника нет, сверять нечем».
    config_path.unlink()
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, state)
    assert excinfo.value.reason == "missing"
    assert excinfo.value.artifact == state.config.path
    config_path.write_bytes(original_config)

    # Симметрично: порча checklists.toml отказывает при исправном config.toml.
    checklists_path.write_bytes(original_checklists + b"\n# external edit\n")
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, state)
    assert excinfo.value.reason == "digest_mismatch"
    assert excinfo.value.artifact == state.checklists.path
    checklists_path.write_bytes(original_checklists)

    # Внутреннее рассогласование самого доказательства: proof заявляет для
    # config.toml digest, которого нет ни у файла на диске, ни у манифеста —
    # оба источника по отдельности целы, но proof о них лжёт.
    forged = _proof_json(stand, state)
    forged["sources"]["config"]["sha256"] = "0" * 64
    forged_state = _rewrite_proof_with_matching_digest(stand, state, forged)
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, forged_state)
    assert excinfo.value.reason == "contradiction"
    assert excinfo.value.artifact == "config"
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(original_proof)

    # Восстановленный proof и снапшоты снова читаются штатно.
    assert load_semantic_proof(pipeline_dir, state) is not None


# ---------------------------------------------------------------------------
# BEH-15 — ошибка доказательства различает безопасные причины
# ---------------------------------------------------------------------------


def test_proof_errors_are_distinct_safe_and_actionable(tmp_path: Path) -> None:
    """Причины различимы программно (`.reason`), диагностика называет только
    идентификатор артефакта (не содержимое) и не предлагает принять живой
    конфиг как новый baseline (BEH-15, FR-15).
    """
    stand = _doc_stand(tmp_path)
    state = stand.manifest()
    pipeline_dir = stand.pipeline_dir()

    missing_ref_state = state.model_copy(update={"semantic_proof": None})
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, missing_ref_state)
    error = excinfo.value

    # Причина — закрытый машиночитаемый код, не свободный текст.
    assert error.reason in {
        "missing",
        "digest_mismatch",
        "parse_error",
        "contradiction",
        "unsupported_version",
    }
    # Артефакт называет идентификатор, а не содержимое — короткая строка
    # без переносов строк, никогда не совпадающая с текстом реального
    # чеклиста или чем-то похожим на содержимое файла.
    assert "\n" not in error.artifact
    assert len(error.artifact) < 80

    message = str(error)
    # Диагностика не раскрывает секретные/содержательные значения источников.
    for checklist in stand.config.checklists.values():
        for text in checklist.texts.values():
            assert text not in message

    # Diagnostic обязан предлагать безопасное действие: восстановить
    # удостоверенные данные либо завершить/пересоздать пайплайн — и НИКОГДА
    # не предлагать принять живой конфиг как новую baseline.
    assert "baseline" in message
    assert "не принимается" in message
    assert "восстанов" in message or "пересоздай" in message.lower()

    # Причины реально различны у разных сценариев, а не единственный код на всё.
    reasons: set[str] = {error.reason}

    config_path = pipeline_dir / state.config.path
    original_config = config_path.read_bytes()
    config_path.write_bytes(original_config + b"\ncorrupted\n")
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, state)
    reasons.add(excinfo.value.reason)
    config_path.write_bytes(original_config)

    garbage = b"not json at all"
    garbage_state = _with_proof_sha256(state, hashlib.sha256(garbage).hexdigest())
    before = _proof_bytes(stand, state)
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(garbage)
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, garbage_state)
    reasons.add(excinfo.value.reason)
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(before)

    unsupported = {**_proof_json(stand, state), "projection_schema_version": "99"}
    unsupported_state = _rewrite_proof_with_matching_digest(stand, state, unsupported)
    with pytest.raises(UnprovableSemantics) as excinfo:
        load_semantic_proof(pipeline_dir, unsupported_state)
    reasons.add(excinfo.value.reason)
    (pipeline_dir / SEMANTIC_PROOF_NAME).write_bytes(before)

    assert reasons == {
        "missing",
        "digest_mismatch",
        "parse_error",
        "unsupported_version",
    }
