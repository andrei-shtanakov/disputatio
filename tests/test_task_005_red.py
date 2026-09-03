"""RED — TASK-005 (WS-disputatio-65): Legacy возобновляется только по явной
доказуемой процедуре (BEH-16).

`PipelineState.semantic_proof` (`contracts/pipeline.py`) документирует себя
как опциональное ровно ради чтения манифестов, записанных до появления
доказательства (issue #65): `None` там читается легитимно, а «что с этим
делать — решает resume (BEH-12/16), не схема». BEH-12 эту половину закрывает
(`load_semantic_proof` отказывает `UnprovableSemantics("missing", ...)`)
безусловно — для ЛЮБОГО манифеста без `semantic_proof`, независимо от того,
достаточно ли на диске уже сохранённых и P9-удостоверенных данных
(`config`/`checklists` снапшотов, на которые сам манифест ссылается и чьи
sha256 не разошлись). BEH-16 требует другого: для КАЖДОЙ поддерживаемой
версии манифеста (здесь — `disputatio/pipeline/v1`, которую `PipelineState`
уже умеет читать нормализацией тега) должна существовать явная процедура
восстановления ожидаемой модели из уже сохранённых после P9 данных — и
только тогда, когда такой процедуры или достаточных данных нет, resume
обязан fail-closed.

Сегодня такой процедуры нет: `PipelineResume._verify_semantics` зовёт
`load_semantic_proof`, которая видит `ref is None` и останавливается ДО
того, как посмотрит, что config.toml/checklists.toml того же легитимного
v1-манифеста никуда не делись и сходятся с зафиксированными в манифесте
digest'ами. Легитимный legacy-пайплайн v0.1 с полностью сохранными данными
сегодня никогда не резюмируется — не потому, что данных недостаточно, а
потому, что explicit-процедура для его версии не реализована.
"""

import json
from pathlib import Path

from runtime._pipeline_stand import SLUG, build_stand, live_pair, start


def test_legacy_manifest_without_semantic_proof_resumes_via_explicit_procedure(
    tmp_path: Path,
) -> None:
    """v1-манифест без `semantic_proof`, но с сохранными P9-снапшотами,
    обязан резюмироваться собственной явной процедурой (BEH-16), а не
    отказывать fail-closed, как будто удостоверенных данных недостаточно.
    """
    stand = build_stand(tmp_path, live_pair())
    start(stand)
    calls_before = len(stand.driver.calls)

    manifest_path = stand.pipeline_dir() / "pipeline.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Приводит манифест к форме, которую писала бы реализация до issue #65:
    # тег v1, без `documents.kind` (§4.2) и без ссылки на доказательство,
    # которого в ту пору не существовало. Снапшоты config/checklists на
    # диске не тронуты — данные, из которых явная процедура v1 обязана
    # восстановить ожидаемую модель, остаются полными и P9-удостоверенными.
    payload["schema"] = "disputatio/pipeline/v1"
    payload["documents"].pop("kind")
    payload.pop("semantic_proof")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    stand.scripts["pair-r1"].outcome = "converged"

    stand.rebuild().resume.resume(SLUG)

    assert len(stand.driver.calls) > calls_before, (
        "легитимный legacy-манифест v1 без semantic_proof, но с полными "
        "P9-удостоверенными снапшотами config/checklists, обязан "
        "продолжиться собственной явной процедурой (BEH-16), а не "
        "остановиться так, будто удостоверенных данных недостаточно"
    )
