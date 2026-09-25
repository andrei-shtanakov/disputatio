"""Read-side зеркало раскладки `.disputatio/` (ADR-002, [REQ-003], [REQ-007]).

Оркестратор обязан **читать** артефакты прошлых раундов, а `disputatio.events`
экспортирует только писателей: `paths` объявлен внутренней деталью раскладки
и в публичный `__init__` не входит, а §4.2 запрещает импорт подмодулей чужих
пакетов. Поэтому здесь — собственный, строго read-only набор функций путей.

Корень путей сессии тот же, что у писателя, — `artifact_root`, журнал
сессии, а не рабочий git-репозиторий (SPEC-002 §4.1); пути каталога
пайплайна (`pipeline_dir_of` и соседи) считаются от `workspace_root`.
Зеркало обязано отражать и это: read-side, считающий пути от рабочего
корня, читал бы историю чужой сессии там, где писатель уже разошёлся.

Дублирование раскладки не декларативно, а проверяемо: `write_round_artifact`
**возвращает** записанный `Path`, и тест шага требует равенства этого пути
пути отсюда для каждого артефакта, который runtime реально пишет. Разъедься
две раскладки — красным станет шаг, а не только этот модуль.

Модуль чист: только конкатенация путей, никакого I/O. Ничего не создаёт и не
проверяет существование — «нет файла» решается вызывающим, потому что для
`changes.patch` это нормальный раунд без правок, а для `review.json` —
оборванная сессия.
"""

from pathlib import Path
from typing import Final

SESSION_DIR_NAME: Final = ".disputatio"
ROUNDS_DIR_NAME: Final = "rounds"
CONFIG_TOML_NAME: Final = "config.toml"
ADOPTED_FINDINGS_NAME: Final = "adopted_findings.json"

PROPOSAL_NAME: Final = "proposal.md"
CHANGES_PATCH_NAME: Final = "changes.patch"
VERIFICATION_NAME: Final = "verification.json"
REVIEW_NAME: Final = "review.json"
DECISION_NAME: Final = "decision.json"

#: Раскладка каталога пайплайна `.disputatio/pipelines/<slug>/` (SPEC-002
#: §4.1) — единственная копия в `runtime`. Писатель манифеста и журнала
#: (`events.pipeline_paths`) держит свою, по тому же правилу, что и для
#: сессии: подмодуль `events` наружу не экспортируется. Общее у двух копий —
#: корень и манифест, их сверяет `tests/runtime/test_pipeline_layout_mirror.py`;
#: `sessions/`, `adoptions/`, `result/` живут только здесь.
PIPELINES_DIR_NAME: Final = "pipelines"
PIPELINE_MANIFEST_NAME: Final = "pipeline.json"
PIPELINE_SESSIONS_DIR_NAME: Final = "sessions"
ADOPTIONS_DIR_NAME: Final = "adoptions"
PIPELINE_RESULT_DIR_NAME: Final = "result"


def session_dir(artifact_root: Path) -> Path:
    """Корневая директория сессии: `artifact_root/.disputatio`."""
    return artifact_root / SESSION_DIR_NAME


def config_toml(artifact_root: Path) -> Path:
    """Путь к снапшоту конфига сессии `config.toml` ([DESIGN-014]).

    Read-side зеркало `events.paths.config_toml_path`: писатель снапшота
    (`write_config_snapshot`) текст только принимает, а resume обязан его
    прочитать — и прочитать оттуда же, куда он записан ([REQ-014]).
    """
    return session_dir(artifact_root) / CONFIG_TOML_NAME


def adopted_findings_json(artifact_root: Path) -> Path:
    """Архитектурные находки, с которыми открыта spec-ревизия (§7.3 SPEC-002).

    Файл уровня сессии, а не раунда: находки принадлежат всей ревизии и
    доезжают до автора КАЖДОГО её раунда. Durable, потому что вычисляются
    один раз при создании ревизии, а читаются в другом процессе — после
    краха `advance` поднимает пайплайн без интента `create_session` на руках
    (он уже исполнен), и восстановить находки из манифеста было бы нечем.
    """
    return session_dir(artifact_root) / ADOPTED_FINDINGS_NAME


def rounds_dir(artifact_root: Path) -> Path:
    """Директория всех раундов `rounds/`."""
    return session_dir(artifact_root) / ROUNDS_DIR_NAME


def round_dir(artifact_root: Path, round_no: int) -> Path:
    """Директория раунда `rounds/NNN` — паддинг тот же, что у писателя."""
    return rounds_dir(artifact_root) / f"{round_no:03d}"


def round_artifact(artifact_root: Path, round_no: int, name: str) -> Path:
    """Путь артефакта `name` внутри раунда `round_no`.

    Имя не валидируется: проверку «простое имя файла» делает писатель, у
    которого от неё зависит инвариант I3, а read-side дублировать чужой
    инвариант не вправе — разъехавшись, две проверки дали бы два разных
    ответа на один вопрос.
    """
    return round_dir(artifact_root, round_no) / name


def pipeline_dir_of(workspace_root: Path, slug: str) -> Path:
    """`.disputatio/pipelines/<slug>` (§4.1) — корень остальных путей пайплайна.

    Корень — `workspace_root` (git-репозиторий), а не `artifact_root`: каталог
    пайплайна лежит в репозитории, а `artifact_root` каждой ревизии — уже
    внутри него. Слаг здесь не валидируется: его грамматику проверяет
    писатель (`events.pipeline_paths.validate_slug`) до создания каталога.
    """
    return workspace_root / SESSION_DIR_NAME / PIPELINES_DIR_NAME / slug


def artifact_root_of(workspace_root: Path, slug: str, session_id: str) -> Path:
    """`artifact_root` одной ревизии: `sessions/<revision>` (§4.1)."""
    return (
        pipeline_dir_of(workspace_root, slug) / PIPELINE_SESSIONS_DIR_NAME / session_id
    )


def adoptions_dir_of(workspace_root: Path, slug: str) -> Path:
    """Каталог канонических патчей принятых внешних правок `adoptions/` (§3.1)."""
    return pipeline_dir_of(workspace_root, slug) / ADOPTIONS_DIR_NAME


def result_dir_of(workspace_root: Path, slug: str) -> Path:
    """Каталог экспорта пайплайна `result/` (§8.2)."""
    return pipeline_dir_of(workspace_root, slug) / PIPELINE_RESULT_DIR_NAME


def result_dir_relative(slug: str) -> str:
    """`result/` относительно `workspace_root` POSIX-строкой (§8.2).

    Строка, а не `Path`: она попадает в текст `publish.txt`, и содержимое
    файла не должно зависеть от разделителя пути ОС, на которой собирался
    экспорт.
    """
    pipelines = f"{SESSION_DIR_NAME}/{PIPELINES_DIR_NAME}"
    return f"{pipelines}/{slug}/{PIPELINE_RESULT_DIR_NAME}"
