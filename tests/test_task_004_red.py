"""RED — TASK-004 (WS-disputatio-65): P9 предшествует чтению ожидаемой семантики.

BEH-11 требует, чтобы drift immutable-проекции живого конфига против
удостоверенного `semantic_proof.json` останавливал `resume` ДО того, как он
тронет сессию хоть одним мутирующим действием (FR-10). Сегодня
`PipelineResume.resume` (`runtime/pipeline_resume.py`) сверяет P9 (control
plane) и вид пайплайна (`_require_same_kind`), но не читает
`semantic_proof.json` и не сравнивает immutable-проекции вовсе:
`load_semantic_proof`/`diff_projections` в `pipeline_semantic_proof.py`
существуют (TASK-001/TASK-002), но никем в порядке §8.1 не вызываются — это
и называет docstring модуля «фактическое встраивание ... — задача TASK-004».

Поэтому живой конфиг, разошедшийся с удостоверенным доказательством в
immutable-поле (`max_architectural_returns`), сегодня молча доезжает до
`PipelineRunner.advance`, и драйвер сессии зовётся снова, хотя BEH-11
запрещает resume продолжать сессию сквозь drift.
"""

import dataclasses
from pathlib import Path

import pytest
from runtime._pipeline_stand import SLUG, build_stand, live_pair, start

from disputatio.runtime.errors import DisputatioError


def test_semantic_drift_in_live_config_stops_resume_before_the_driver_runs(
    tmp_path: Path,
) -> None:
    """Живой конфиг с изменённым immutable-полем обязан остановить `resume`."""
    stand = build_stand(tmp_path, live_pair())
    start(stand)
    calls_before = len(stand.driver.calls)

    drifted = dataclasses.replace(
        stand.config,
        max_architectural_returns=stand.config.max_architectural_returns + 1,
    )

    with pytest.raises(DisputatioError):
        stand.resume_with(drifted).resume(SLUG)

    assert len(stand.driver.calls) == calls_before, (
        "resume продолжил сессию сквозь semantic drift в "
        "max_architectural_returns — BEH-11 требует остановки ДО запуска "
        "или возобновления сессии"
    )
