"""Раскладка пайплайна в `runtime.layout` совпадает с писателем `events` (#115).

Две копии корня `.disputatio/pipelines/<slug>/` остаются намеренно:
`events.pipeline_paths` наружу пакетом не экспортируется (ADR-002, тот же
довод, что у пары `events.paths` ↔ `runtime.layout` для сессии), а `runtime`
обязан читать манифест и журнал там, где их записал `events`. Этот тест —
то, что делает дублирование проверяемым: переименование каталога или файла
в одной копии без другой краснит его, а не расходится молча.

Путь журнала пайплайна здесь не сверяется: `runtime` его не строит, а
получает инъекцией (`PipelineEventSink.path`, см. `composition`).
"""

from pathlib import Path

from disputatio.events import pipeline_paths
from disputatio.runtime.layout import PIPELINE_MANIFEST_NAME, pipeline_dir_of

SLUG = "demo-slug"


def test_pipeline_dir_matches_writer(tmp_path: Path) -> None:
    """Корень каталога пайплайна одинаков у писателя и у `runtime`."""
    assert pipeline_dir_of(tmp_path, SLUG) == pipeline_paths.pipeline_dir(
        tmp_path, SLUG
    )


def test_manifest_path_matches_writer(tmp_path: Path) -> None:
    """`runtime` ищет `pipeline.json` там, куда его пишет `pipeline_store`."""
    expected = pipeline_paths.manifest_path(tmp_path, SLUG)
    assert pipeline_dir_of(tmp_path, SLUG) / PIPELINE_MANIFEST_NAME == expected
