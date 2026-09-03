"""Версионированное доказательство immutable-проекции `[pipeline]` (WS-disputatio-65).

Milestone 1 (disputatio#65) называет разрыв: `resume` сегодня сверяет с
манифестом только вид пайплайна (P0) и берёт чеклист/пути документов из
ЖИВОГО конфига (`pipeline_config._operator_checklist`, «известное
ограничение issue #65») — конфиг, изменённый между запусками, доезжает до
ревьюера, хотя манифест удостоверяет хеш прежнего снапшота. Закрытие разрыва
идёт очередью задач; этот модуль закрывает ровно её первый шаг (BEH-01,
BEH-12, BEH-13, BEH-15):

* **BEH-01** — `run` до первой сессии обязан зафиксировать версионированное
  доказательство итоговой immutable-проекции (`write_semantic_proof`,
  вызывается `PipelineRunner.run` до первой записи `pipeline.json` — тем же
  приёмом, каким уже фиксируются `task.md`/`config.toml`/`checklists.toml`):
  каждый снапшот пишется своей отдельной атомарной операцией, но все они —
  включая `semantic_proof.json` — обязаны лечь на диск до манифеста, чтобы
  первая же запись манифеста уже несла на него ссылку.
* **BEH-12/BEH-13/BEH-15** — `load_semantic_proof` восстанавливает эту
  проекцию на `resume` fail-closed: недоказуемость (отсутствие, порча digest,
  ошибка разбора, несовместимая версия, внутреннее противоречие) любого
  ЗАДЕЙСТВОВАННОГО источника (сам proof, `config.toml`, `checklists.toml`)
  останавливает восстановление без fallback на живой конфиг и без
  автоматической записи/починки — по одной причине `errors.SemanticProofReason`
  на артефакт, без утечки его содержимого в диагностику.

Фактическое встраивание `load_semantic_proof` в порядок §8.1 `resume`
(P9 → манифест → semantic proof → …) и сравнение с живой моделью — задачи
TASK-002…TASK-004 того же milestone; здесь доказательство только строится и
проверяется как самостоятельный, полностью тестируемый шаг.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from disputatio.contracts import FileRef, PipelineKind, PipelineState
from disputatio.events import atomic_write
from disputatio.runtime.errors import UnprovableSemantics
from disputatio.runtime.pipeline_config import PipelineConfig

#: Имя файла доказательства внутри `.disputatio/pipelines/<slug>/` (§4.1).
SEMANTIC_PROOF_NAME: Final = "semantic_proof.json"

#: Версия канонизации immutable-проекции (NFR-02): хранится ВМЕСТЕ с
#: доказательством, а не выводится из версии кода — иначе апдейт приложения
#: либо пересчитывал бы старое доказательство новым алгоритмом, либо объявлял
#: бы drift только из-за смены версии, что FR-19/NFR-02 запрещают явно.
PROJECTION_SCHEMA_VERSION: Final = "1"

#: Поддерживаемые версии канонизации этой версией кода. Множество, а не
#: единственное значение: NFR-02 разрешает будущей версии кода понимать
#: несколько версий канонизации разом (для чтения старых доказательств).
SUPPORTED_PROJECTION_SCHEMA_VERSIONS: Final = frozenset({PROJECTION_SCHEMA_VERSION})

#: Источники, которые обязано назвать доказательство (BEH-13): снапшоты,
#: из которых собрана immutable-проекция. `task.md` сюда не входит — задача
#: не входит в закрытую immutable-классификацию `[pipeline]` (WS-65
#: requirements, «Закрытая классификация»), она несёт текст запроса, а не
#: настройку контура.
_PROOF_SOURCES: Final = ("config", "checklists")


def build_projection(config: PipelineConfig) -> dict[str, Any]:
    """Каноническая immutable-проекция `[pipeline]` — закрытая таблица WS-65.

    Ровно immutable-поля закрытой классификации требований WS-disputatio-65:
    `kind`, пути документов формы, `max_architectural_returns` (только
    `pair`), чеклисты обоих применимых контуров и упорядоченные `extra_gates`
    со всеми свойствами. `soft_max_pipeline_tokens`,
    `soft_max_pipeline_wall_seconds`, `protected_branches` и `anchor_path` —
    mutable по той же таблице и в проекцию не входят: их правка не обязана
    порождать semantic drift (FR-06).

    Полная канонизация путей и TOML-эквивалентности (не зависеть от того, как
    записан default, от порядка незначимых таблиц и т.п. — FR-02, FR-03) —
    предмет отдельной задачи очереди (TASK-002); здесь `PipelineConfig` уже
    разобран и провалидирован, и его поля берутся как есть.
    """
    projection: dict[str, Any] = {"kind": config.kind.value}
    if config.kind is PipelineKind.DOCUMENT:
        (document_path,) = config.documents()
        projection["document_path"] = document_path
    else:
        spec_path, plan_path = config.documents()
        projection["spec_path"] = spec_path
        projection["plan_path"] = plan_path
        projection["max_architectural_returns"] = config.max_architectural_returns
    projection["checklists"] = {
        contour: {
            "order": list(checklist.order),
            "texts": dict(checklist.texts),
            "findings_item": checklist.findings_item,
        }
        for contour, checklist in sorted(config.checklists.items())
    }
    projection["gates"] = [
        {"name": gate.name, "cmd": gate.cmd, "enabled": gate.enabled}
        for gate in config.extra_gates
    ]
    return projection


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Детерминированная сериализация (NFR-01): сортированные ключи, без пробелов.

    Одинаковый разобранный payload обязан давать идентичные байты на всех
    поддерживаемых платформах — `sort_keys` снимает зависимость от порядка
    вставки словаря, компактные разделители снимают зависимость от версии
    `json`, кодировка зафиксирована явно.
    """
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def write_semantic_proof(
    directory: Path,
    *,
    pipeline_id: str,
    config: PipelineConfig,
    config_ref: FileRef,
    checklists_ref: FileRef,
) -> FileRef:
    """Пишет `semantic_proof.json` и отдаёт `FileRef` для манифеста (BEH-01, FR-01).

    Вызывающий (`PipelineRunner.run`) обязан вызвать эту функцию ДО первой
    записи `pipeline.json` и передать её результат полем `semantic_proof`
    ТОЙ ЖЕ конструкции состояния — доказательство ложится на диск своей
    отдельной атомарной записью, но в то же некоммитнутое окно, что и
    `task.md`/`config.toml`/`checklists.toml`, до манифеста, так что первая
    же запись манифеста с первым intent'ом уже способна нести на него ссылку
    (BEH-01).

    `config_ref`/`checklists_ref` — те же `FileRef`, что и `state.config`/
    `state.checklists` манифеста: доказательство удостоверяет РОВНО эти два
    снапшота, а не пересчитывает их заново, поэтому расхождение между тем,
    что скажет `sources` при чтении, и тем, что несёт манифест, обязано быть
    видимым при проверке (BEH-13), а не совпадать по построению по третьему
    независимому вычислению тех же байтов.
    """
    payload = {
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "pipeline_id": pipeline_id,
        "projection": build_projection(config),
        "sources": {
            "config": {"path": config_ref.path, "sha256": config_ref.sha256},
            "checklists": {
                "path": checklists_ref.path,
                "sha256": checklists_ref.sha256,
            },
        },
    }
    data = _canonical_json(payload)
    atomic_write(directory / SEMANTIC_PROOF_NAME, data)
    return FileRef(path=SEMANTIC_PROOF_NAME, sha256=hashlib.sha256(data).hexdigest())


def load_semantic_proof(pipeline_dir: Path, state: PipelineState) -> Mapping[str, Any]:
    """Восстанавливает и проверяет proof fail-closed (BEH-12, BEH-13, BEH-15).

    Единственный вход, которым `resume` (в будущей задаче очереди) вправе
    получить ожидаемую immutable-проекцию. Каждая проверка ниже — отдельная,
    поимённо диагностируемая причина `errors.SemanticProofReason`, и первая
    же неудача останавливает восстановление целиком: ни частично доверенных
    данных, ни fallback на живой конфиг, ни попытки починить/переписать
    doказательство здесь нет ни в одной ветке (FR-11, FR-12).

    Источники BEH-13 (`semantic_proof.json`, `config.toml`, `checklists.toml`)
    проверяются НЕЗАВИСИМО друг от друга — совпадение живой модели с одним
    источником не освобождает от проверки остальных, и повреждение одного
    отказывает, даже если все прочие сошлись, потому что для этого проверки
    не делят между собой ни один вывод: расхождение доказательства с
    манифестом о том, какой файл и с каким digest оно удостоверяет, — тоже
    самостоятельная причина (`contradiction`), а не производная от прочих.
    """
    ref = state.semantic_proof
    if ref is None:
        raise UnprovableSemantics(
            "missing",
            "semantic_proof",
            "манифест не несёт ссылки на доказательство immutable-проекции",
        )
    proof_path = pipeline_dir / ref.path
    try:
        data = proof_path.read_bytes()
    except OSError:
        raise UnprovableSemantics(
            "missing",
            ref.path,
            "файл, названный манифестом, отсутствует либо не читается",
        ) from None
    if hashlib.sha256(data).hexdigest() != ref.sha256:
        raise UnprovableSemantics(
            "digest_mismatch",
            ref.path,
            "sha256 файла не совпадает с зафиксированным в манифесте",
        )
    try:
        proof = json.loads(data)
    except json.JSONDecodeError:
        raise UnprovableSemantics(
            "parse_error", ref.path, "содержимое не разбирается как JSON"
        ) from None
    if not isinstance(proof, Mapping):
        raise UnprovableSemantics(
            "parse_error", ref.path, "верхний уровень доказательства — не объект"
        )
    version = proof.get("projection_schema_version")
    if (
        not isinstance(version, str)
        or version not in SUPPORTED_PROJECTION_SCHEMA_VERSIONS
    ):
        # Само значение версии НЕ воспроизводится (BEH-15/FR-15, приёмка
        # PR #90): поле читается из недоверенного артефакта, и порченый
        # proof мог бы вынести в терминал/лог произвольное содержимое.
        raise UnprovableSemantics(
            "unsupported_version",
            ref.path,
            "версия канонизации не входит в поддерживаемые этой версией "
            f"кода: {sorted(SUPPORTED_PROJECTION_SCHEMA_VERSIONS)}",
        )
    if proof.get("pipeline_id") != state.pipeline_id:
        raise UnprovableSemantics(
            "contradiction",
            ref.path,
            "доказательство называет другой pipeline_id, чем манифест — "
            "оно не может относиться к этому пайплайну",
        )
    if not isinstance(proof.get("projection"), Mapping):
        raise UnprovableSemantics(
            "parse_error", ref.path, "доказательство не несёт immutable-проекцию"
        )
    sources = proof.get("sources")
    if not isinstance(sources, Mapping):
        raise UnprovableSemantics(
            "parse_error", ref.path, "доказательство не называет свои источники"
        )
    for name in _PROOF_SOURCES:
        _verify_source(pipeline_dir, sources, name, getattr(state, name))
    return proof


def _verify_source(
    pipeline_dir: Path,
    sources: Mapping[str, Any],
    name: str,
    manifest_ref: FileRef,
) -> None:
    """Один источник BEH-13: заявка доказательства ↔ манифест ↔ диск.

    Три независимые проверки одного источника, каждая со своей причиной:
    заявка доказательства структурно негодна (`parse_error`), заявка
    расходится с тем, что несёт манифест об этом же снапшоте
    (`contradiction`), либо байты снапшота на диске не совпадают с
    удостоверенным digest (`missing`/`digest_mismatch`). Имя источника
    (`config`/`checklists`), а не путь файла — то, что называет диагностика
    на первых двух причинах: до сверки с манифестом файлу, названному
    доказательством, верить нельзя (WS-65 requirements FR-08).
    """
    entry = sources.get(name)
    if not isinstance(entry, Mapping):
        raise UnprovableSemantics(
            "parse_error", name, "доказательство не описывает этот источник"
        )
    path_value, sha_value = entry.get("path"), entry.get("sha256")
    if not isinstance(path_value, str) or not isinstance(sha_value, str):
        raise UnprovableSemantics(
            "parse_error", name, "запись источника несёт поля не тех типов"
        )
    if path_value != manifest_ref.path or sha_value != manifest_ref.sha256:
        raise UnprovableSemantics(
            "contradiction",
            name,
            "доказательство расходится с манифестом о том, какой файл и "
            "с каким digest оно удостоверяет",
        )
    source_path = pipeline_dir / manifest_ref.path
    try:
        actual = source_path.read_bytes()
    except OSError:
        raise UnprovableSemantics(
            "missing",
            manifest_ref.path,
            "снапшот источника отсутствует либо не читается",
        ) from None
    if hashlib.sha256(actual).hexdigest() != manifest_ref.sha256:
        raise UnprovableSemantics(
            "digest_mismatch",
            manifest_ref.path,
            "содержимое снапшота на диске не совпадает с удостоверенным digest",
        )
