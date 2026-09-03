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

Встраивание `load_semantic_proof` в порядок §8.1 `resume` (P9 → манифест →
semantic proof → …) сделано в `pipeline_resume.PipelineResume._verify_semantics`
(BEH-09/10/11/17/20, TASK-004); здесь доказательство по-прежнему строится и
проверяется как самостоятельный, полностью тестируемый шаг — `resume` лишь
вызывает уже готовые `load_semantic_proof`/`diff_projections`, не дублируя их
логику. Само сравнение двух проекций (`diff_projections`, BEH-02/04-07/14/19,
TASK-002) не зависит от порядка `resume` и тестируется без него.
"""

import hashlib
import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

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

#: Классификация полей `PipelineConfig` — единственная декларативная схема
#: (NFR-08), по которой сверяются `build_projection` (что попадает в
#: проекцию) и тест `test_schema_parser_and_canonicalizer_classifications_match`
#: (BEH-21): dataclass-поле без записи здесь ломает тест, а не тихо
#: становится mutable по умолчанию (FR-07). `extra_gates`/`checklists` —
#: имена ПОЛЕЙ dataclass'а, а не ключи проекции (`gates` в `build_projection`
#: ниже) — тест сверяет множество имён, а не написание.
FieldClass = Literal["immutable", "mutable"]
PIPELINE_CONFIG_FIELD_CLASS: Final[dict[str, FieldClass]] = {
    "kind": "immutable",
    "spec_path": "immutable",
    "plan_path": "immutable",
    "document_path": "immutable",
    "max_architectural_returns": "immutable",
    "checklists": "immutable",
    "extra_gates": "immutable",
    "soft_max_pipeline_tokens": "mutable",
    "soft_max_pipeline_wall_seconds": "mutable",
    "protected_branches": "mutable",
    "anchor_path": "mutable",
}


def build_projection(config: PipelineConfig) -> dict[str, Any]:
    """Каноническая immutable-проекция `[pipeline]` — закрытая таблица WS-65.

    Ровно immutable-поля закрытой классификации требований WS-disputatio-65
    (`PIPELINE_CONFIG_FIELD_CLASS` выше): `kind`, пути документов формы,
    `max_architectural_returns` (только `pair`), чеклисты обоих применимых
    контуров и упорядоченные `extra_gates` со всеми свойствами.
    `soft_max_pipeline_tokens`, `soft_max_pipeline_wall_seconds`,
    `protected_branches` и `anchor_path` — mutable по той же таблице и в
    проекцию не входят: их правка не обязана порождать semantic drift
    (FR-06, BEH-07) — не потому, что `diff_projections` их прощает, а
    потому, что сравнивать здесь попросту нечего.

    Канонизация путей без машинной привязки (BEH-03, FR-03) и TOML-
    эквивалентность формата (BEH-02, FR-02) — не забота ЭТОЙ функции:
    `PipelineConfig` на входе уже разобран и провалидирован
    (`pipeline_config.load_pipeline_config` → `validate_relative_path`),
    и оба свойства — следствие того, ЧТО она принимает, а не отдельная
    логика внутри неё. `diff_projections` ниже сравнивает уже канонические
    словари, которые вернула эта функция.
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


@dataclass(frozen=True, slots=True)
class ProjectionDiff:
    """Одно расхождение между ожидаемой и живой immutable-проекциями (BEH-14).

    `field` — отсортируемый канонический путь поля (`"plan_path"`,
    `"checklists.pair.texts.P1"`, `"gates[1].cmd"`) — то, что диагностика
    вправе напечатать всегда (FR-14). `old`/`new` несут исходное и живое
    значение ТОЛЬКО для полей, у которых это безопасно (enum, число,
    относительный путь документа, порядок id, роль чеклиста) — для текста
    пункта чеклиста и команды gate'а они остаются `None` независимо от
    реальных значений: ни одно из этих двух полей не принимает `None`
    как содержательное значение (`build_projection` пишет туда строки),
    поэтому пропуск однозначно читается как «значение скрыто», а не как
    «оно и было пустым» (BEH-14, FR-14 — команды gates и тексты
    prompt/checklist не печатаются).
    """

    field: str
    old: Any = None
    new: Any = None


def diff_projections(
    expected: Mapping[str, Any], live: Mapping[str, Any]
) -> list[ProjectionDiff]:
    """Semantic diff двух канонических проекций (BEH-02/04-07/14/19, FR-02).

    Сравниваются РОВНО поля, которые несёт `build_projection`: mutable
    controls в проекцию не попадают вовсе (BEH-07), поэтому их не нужно
    отдельно исключать здесь — исключать нечего. Пустой список — отсутствие
    semantic drift: конфиги, различающиеся только форматированием TOML
    (комментарии, пробелы, стиль кавычек, порядок незначимых таблиц,
    явная запись значения, равного default — BEH-02), дают структурно
    ОДИНАКОВЫЕ проекции уже на этапе `load_pipeline_config`/`build_projection`,
    поэтому этой функции остаётся только структурное сравнение уже
    канонизированных словарей.

    Результат отсортирован по `field` (BEH-14, FR-14: «отсортированный
    набор различающихся канонических путей») — порядок не зависит ни от
    порядка вставки Python-словаря, ни от того, expected или live
    сравнивались первыми.
    """
    diffs: list[ProjectionDiff] = []
    for field in ("kind", "spec_path", "plan_path", "document_path"):
        if field in expected or field in live:
            diffs.extend(_diff_scalar(field, expected.get(field), live.get(field)))
    if "max_architectural_returns" in expected or "max_architectural_returns" in live:
        diffs.extend(
            _diff_scalar(
                "max_architectural_returns",
                expected.get("max_architectural_returns"),
                live.get("max_architectural_returns"),
            )
        )
    diffs.extend(
        _diff_checklists(expected.get("checklists", {}), live.get("checklists", {}))
    )
    diffs.extend(_diff_gates(expected.get("gates", ()), live.get("gates", ())))
    return sorted(diffs, key=lambda diff: diff.field)


def _diff_scalar(field: str, old: Any, new: Any) -> list[ProjectionDiff]:
    """Один безопасный (не текстовый, не gate-command) лист проекции."""
    if old == new:
        return []
    return [ProjectionDiff(field=field, old=old, new=new)]


def _diff_checklists(
    expected: Mapping[str, Any], live: Mapping[str, Any]
) -> list[ProjectionDiff]:
    """Полная семантика чеклистов immutable — BEH-04 (`spec`/`pair`) и BEH-05 (`doc`).

    Итерация идёт по ОБЪЕДИНЕНИЮ контуров обеих проекций, а не по одной из
    них: контур, присутствующий только с одной стороны (смена вида —
    BEH-18/FR-18), — тоже drift, и заметить его может только объединение.
    `order` и `findings_item` несут id, а не текст, — им можно называть
    старое/новое значение; `texts.<id>` — предмет BEH-14: путь называется,
    содержимое текста пункта — никогда (см. `ProjectionDiff`).
    """
    diffs: list[ProjectionDiff] = []
    for contour in sorted(set(expected) | set(live)):
        expected_checklist = expected.get(contour, {})
        live_checklist = live.get(contour, {})
        diffs.extend(
            _diff_scalar(
                f"checklists.{contour}.order",
                expected_checklist.get("order"),
                live_checklist.get("order"),
            )
        )
        diffs.extend(
            _diff_scalar(
                f"checklists.{contour}.findings_item",
                expected_checklist.get("findings_item"),
                live_checklist.get("findings_item"),
            )
        )
        expected_texts = expected_checklist.get("texts", {})
        live_texts = live_checklist.get("texts", {})
        for item_id in sorted(set(expected_texts) | set(live_texts)):
            if expected_texts.get(item_id) != live_texts.get(item_id):
                diffs.append(
                    ProjectionDiff(field=f"checklists.{contour}.texts.{item_id}")
                )
    return diffs


def _diff_gates(
    expected: Sequence[Mapping[str, Any]], live: Sequence[Mapping[str, Any]]
) -> list[ProjectionDiff]:
    """Упорядоченный список `extra_gates` и все их свойства — BEH-06.

    Индекс — часть пути: перестановка двух gate'ов меняет, что стоит по
    каждому индексу, и уже поэтому является drift (FR-05), а не только
    смена одного свойства на месте. Gate, присутствующий только у одной
    стороны (добавление/удаление), сравнивается с "пустой" другой стороной
    (`None`) — те же безопасные/редактируемые правила, что и у равных по
    длине списков. `cmd` — предмет BEH-14 (путь называется, команда не
    печатается); `name`/`enabled` безопасны.
    """
    diffs: list[ProjectionDiff] = []
    for index in range(max(len(expected), len(live))):
        expected_gate = expected[index] if index < len(expected) else None
        live_gate = live[index] if index < len(live) else None
        diffs.extend(
            _diff_scalar(
                f"gates[{index}].name",
                expected_gate.get("name") if expected_gate else None,
                live_gate.get("name") if live_gate else None,
            )
        )
        expected_cmd = expected_gate.get("cmd") if expected_gate else None
        live_cmd = live_gate.get("cmd") if live_gate else None
        if expected_cmd != live_cmd:
            diffs.append(ProjectionDiff(field=f"gates[{index}].cmd"))
        diffs.extend(
            _diff_scalar(
                f"gates[{index}].enabled",
                expected_gate.get("enabled") if expected_gate else None,
                live_gate.get("enabled") if live_gate else None,
            )
        )
    return diffs


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
    # UnicodeDecodeError наравне с JSONDecodeError (приёмка PR #90,
    # круг 2): json.loads над сырыми байтами падает ИМ на невалидном
    # UTF-8, и сырое исключение обошло бы контрактную диагностику
    # parse_error (BEH-15) — тот же класс, что закрыт в integrity-журнале
    # WS-57 (K2).
    try:
        proof = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
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
    # Структура проекции валидируется ДО того, как её увидит
    # diff_projections (приёмка PR #93, круг 2, minor): происхождение —
    # файл в недоверенном дереве, и негодные типы полей (checklists-строка,
    # gates-маппинг) роняли бы сравнение сырым AttributeError/KeyError мимо
    # именованных причин BEH-15.
    projection = proof["projection"]
    checklists = projection.get("checklists")
    if checklists is not None and (
        not isinstance(checklists, Mapping)
        or not all(isinstance(c, Mapping) for c in checklists.values())
    ):
        raise UnprovableSemantics(
            "parse_error",
            ref.path,
            "проекция доказательства несёт checklists не той структуры",
        )
    gates = projection.get("gates")
    if gates is not None and (
        not isinstance(gates, list) or not all(isinstance(g, Mapping) for g in gates)
    ):
        raise UnprovableSemantics(
            "parse_error",
            ref.path,
            "проекция доказательства несёт gates не той структуры",
        )
    sources = proof.get("sources")
    if not isinstance(sources, Mapping):
        raise UnprovableSemantics(
            "parse_error", ref.path, "доказательство не называет свои источники"
        )
    for name in _PROOF_SOURCES:
        _verify_source(pipeline_dir, sources, name, getattr(state, name))
    return proof


def _read_verified_snapshot(pipeline_dir: Path, ref: FileRef) -> bytes:
    """Байты снапшота, сверенные по digest с его собственной ссылкой (BEH-13/16).

    Общий последний шаг обоих читателей immutable-проекции
    (`_verify_source` для `load_semantic_proof`, `load_legacy_projection`
    напрямую): файл `ref.path` внутри каталога пайплайна обязан
    существовать и совпадать по sha256 с `ref.sha256`. Чья это ссылка —
    заявка доказательства (уже сверенная с манифестом выше по стеку) или
    поле самого манифеста (`state.config`/`state.checklists` — вход
    `load_legacy_projection`, где proof-заявки сверять не с чем) — этой
    проверке безразлично, отсюда и общий код.
    """
    source_path = pipeline_dir / ref.path
    try:
        data = source_path.read_bytes()
    except OSError:
        raise UnprovableSemantics(
            "missing", ref.path, "снапшот источника отсутствует либо не читается"
        ) from None
    if hashlib.sha256(data).hexdigest() != ref.sha256:
        raise UnprovableSemantics(
            "digest_mismatch",
            ref.path,
            "содержимое снапшота на диске не совпадает с удостоверенным digest",
        )
    return data


def _parse_toml_snapshot(name: str, data: bytes, path: str) -> Mapping[str, Any]:
    """Разбор+предметная схема снапшота — общий хвост BEH-13/16.

    Сходящийся digest (`_read_verified_snapshot`) удостоверяет байты, но не
    их годность: снапшот, не разбирающийся как TOML-маппинг ожидаемой формы,
    нельзя трактовать как доказанный источник ни доказательству (BEH-13),
    ни явной legacy-процедуре (BEH-16) — обе останавливаются одной и той же
    причиной. Содержимое в диагностику не выносится (BEH-15).
    """
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise UnprovableSemantics(
            "parse_error", path, "снапшот источника не разбирается как TOML"
        ) from None
    if not isinstance(parsed, Mapping) or not parsed:
        raise UnprovableSemantics(
            "parse_error", path, "снапшот источника пуст или не несёт TOML-маппинг"
        )
    _validate_source_schema(name, parsed, path)
    return parsed


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
    actual = _read_verified_snapshot(pipeline_dir, manifest_ref)
    _parse_toml_snapshot(name, actual, manifest_ref.path)


def _validate_source_schema(name: str, parsed: Mapping[str, Any], path: str) -> None:
    """Предметная схема снапшота (FR-12, приёмка PR #90, круг 6).

    Digest удостоверяет байты, общий TOML-разбор — синтаксис; произвольный
    валидный TOML (`[unrelated]`) всё ещё не является снапшотом источника.
    Якоря — инварианты собственных писателей (`pipeline_runner`):
    config-снапшот несёт таблицу ``[pipeline]``; checklists-снапшот — по
    таблице на контур, каждая с обязательным ``findings_item`` (пустая
    роль пишется явным ``false``, не пропуском). Глубже — модельная
    валидация expected-модели, объём TASK-002+.
    """
    if name == "config":
        if not isinstance(parsed.get("pipeline"), Mapping):
            raise UnprovableSemantics(
                "parse_error",
                path,
                "config-снапшот не несёт таблицу [pipeline]",
            )
        return
    if name == "checklists":
        contours = list(parsed.values())
        if not contours or not all(
            isinstance(contour, Mapping) and "findings_item" in contour
            for contour in contours
        ):
            raise UnprovableSemantics(
                "parse_error",
                path,
                "checklists-снапшот не несёт контуров с findings_item",
            )


# ---------------------------------------------------------------------------
# BEH-16 — legacy-манифест без semantic_proof: явная процедура, не fallback
# ---------------------------------------------------------------------------


def load_legacy_projection(
    pipeline_dir: Path, state: PipelineState
) -> Mapping[str, Any]:
    """Явная процедура восстановления immutable-проекции без `semantic_proof`.

    Единственный второй вход `resume`'а (наряду с `load_semantic_proof`) —
    для манифеста, записанного до появления доказательства (issue #65,
    `state.semantic_proof is None`, FR-16). Обе формы, которые сегодня
    читает `PipelineState` без proof — v1 (документы без `kind`, поднятые
    до пары нормализацией тега, `contracts.pipeline._normalize_v1_documents`)
    и ранний v2 (документы уже с `kind`, но ещё без proof — окно между
    поддержкой вида `document` и TASK-001 этой же очереди) — закрыты ОДНОЙ
    процедурой: к моменту, когда `resume` видит `state`, разница уже стёрта
    нормализацией, и вид с путями документов у обеих читаются из одного и
    того же поля манифеста, а не из тега схемы. Третьей, неподдерживаемой
    версии здесь взяться неоткуда: `schema_` — закрытый `Literal` пары
    значений, и файл с любым другим тегом отказывает ещё в `PipelineState`,
    до того как эта функция вообще вызвана.

    Источник доверия — те же снапшоты, что удостоверяет
    `write_semantic_proof` у современных пайплайнов (`config.toml`,
    `checklists.toml`), сверенные по digest с ссылками САМОГО манифеста
    (`_read_verified_snapshot` — та же проверка, что видит каждый источник
    BEH-13; заявки proof здесь сверять не с чем, ссылка манифеста и есть
    доверенный вход). Их байты писал детерминированный
    `PipelineRunner._config_snapshot`/`_checklists_snapshot`: разбор ниже —
    обратная сторона той же записи, а не отдельно живущий формат, и
    `test_schema_parser_and_canonicalizer_classifications_match` (BEH-21)
    ловит их расхождение.

    Вид и пути документов при этом берутся НЕ из снапшота, а из
    `state.documents` — того же поля манифеста, что и P0 сверял до
    появления доказательства (§4.2, «вид неизменяем»): снапшот удостоверяет
    то, чего в манифесте нет (гейты, `max_architectural_returns`,
    чеклисты), а не дублирует то, что там уже есть.

    Недостаточность данных (отсутствующий/повреждённый снапшот, форма не
    та, что пишет `pipeline_runner`) — тот же `UnprovableSemantics`, что и у
    `load_semantic_proof`: ни in-place дозаписи `semantic_proof.json`, ни
    предположения эквивалентности по одному `documents.kind`, ни другого
    fallback здесь нет ни в одной ветке (FR-16).
    """
    config_data = _read_verified_snapshot(pipeline_dir, state.config)
    checklists_data = _read_verified_snapshot(pipeline_dir, state.checklists)
    config_table = _parse_toml_snapshot("config", config_data, state.config.path)
    checklists_table = _parse_toml_snapshot(
        "checklists", checklists_data, state.checklists.path
    )
    projection = _legacy_projection(state, config_table, checklists_table)
    return {
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "pipeline_id": state.pipeline_id,
        "projection": projection,
    }


def _legacy_projection(
    state: PipelineState,
    config_table: Mapping[str, Any],
    checklists_table: Mapping[str, Any],
) -> dict[str, Any]:
    """Проекция той же формы, что и `build_projection` (BEH-16), из снапшотов."""
    pipeline_table = config_table["pipeline"]
    projection: dict[str, Any] = {"kind": state.kind.value}
    if state.kind is PipelineKind.DOCUMENT:
        (document_path,) = state.documents.paths()
        projection["document_path"] = document_path
    else:
        spec_path, plan_path = state.documents.paths()
        projection["spec_path"] = spec_path
        projection["plan_path"] = plan_path
        projection["max_architectural_returns"] = _legacy_max_returns(pipeline_table)
    projection["checklists"] = _legacy_checklists(checklists_table)
    projection["gates"] = _legacy_gates(pipeline_table)
    return projection


def _legacy_max_returns(pipeline_table: Mapping[str, Any]) -> int:
    """`max_architectural_returns` снапшота — обязано быть целым (BEH-16)."""
    value = pipeline_table.get("max_architectural_returns")
    if not isinstance(value, int) or isinstance(value, bool):
        raise UnprovableSemantics(
            "parse_error",
            "config",
            "config-снапшот не несёт целочисленный max_architectural_returns",
        )
    return value


def _legacy_gates(pipeline_table: Mapping[str, Any]) -> list[dict[str, Any]]:
    """`[[pipeline.gates]]` снапшота — форма `build_projection.gates` (BEH-16)."""
    raw = pipeline_table.get("gates", [])
    if not isinstance(raw, list):
        raise UnprovableSemantics(
            "parse_error", "config", "config-снапшот несёт gates не той структуры"
        )
    gates: list[dict[str, Any]] = []
    for gate in raw:
        if (
            not isinstance(gate, Mapping)
            or not isinstance(gate.get("name"), str)
            or not isinstance(gate.get("cmd"), str)
            or not isinstance(gate.get("enabled"), bool)
        ):
            raise UnprovableSemantics(
                "parse_error", "config", "config-снапшот несёт gate не той структуры"
            )
        gates.append(
            {"name": gate["name"], "cmd": gate["cmd"], "enabled": gate["enabled"]}
        )
    return gates


def _legacy_checklists(checklists_table: Mapping[str, Any]) -> dict[str, Any]:
    """Контуры чеклистов снапшота — форма `build_projection.checklists` (BEH-16).

    `order`/`texts` читаются из порядка ключей уже разобранной TOML-таблицы
    (`tomllib` сохраняет порядок объявления файла): для встроенных контуров
    `pipeline_runner._checklists_snapshot` пишет их отсортированными по id,
    что для вендоренного набора (`S1..S5`, `P1..P5`) совпадает с порядком
    живого конфига по построению; для операторского `doc` файл несёт
    порядок объявления как есть — тот же порядок, что и у живого конфига.
    """
    result: dict[str, Any] = {}
    for contour, entry in checklists_table.items():
        if not isinstance(entry, Mapping):
            raise UnprovableSemantics(
                "parse_error",
                "checklists",
                "checklists-снапшот несёт контур не той структуры",
            )
        role = entry.get("findings_item")
        findings_item = role if isinstance(role, str) else None
        texts: dict[str, str] = {}
        for item_id, text in entry.items():
            if item_id == "findings_item":
                continue
            if not isinstance(text, str):
                raise UnprovableSemantics(
                    "parse_error",
                    "checklists",
                    "checklists-снапшот несёт текст пункта не той структуры",
                )
            texts[item_id] = text
        result[contour] = {
            "order": list(texts),
            "texts": texts,
            "findings_item": findings_item,
        }
    return result
