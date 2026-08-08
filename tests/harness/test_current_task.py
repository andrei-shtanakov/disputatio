"""Резолвер текущей задачи: ровно один IN_PROGRESS/REVIEW по всем spec/*tasks.md."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import tdd_gate


def write_tasks(root: Path, name: str, body: str) -> None:
    spec = root / "spec"
    spec.mkdir(exist_ok=True)
    (spec / name).write_text(body)


ONE_RUNNING = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
"""


def test_single_in_progress(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_maestro_prefix_file(tmp_path: Path) -> None:
    write_tasks(tmp_path, "maestro-tasks.md", ONE_RUNNING)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_review_status_counts(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "🔍 REVIEW"))
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_plain_format_without_emoji(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "IN_PROGRESS"))
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_zero_running_is_error(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING.replace("🔄 IN_PROGRESS", "⬜ TODO"))
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_two_running_is_error(tmp_path: Path) -> None:
    body = ONE_RUNNING.replace("⬜ TODO", "🔄 IN_PROGRESS")
    write_tasks(tmp_path, "tasks.md", body)
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_two_files_one_running_each_is_error(tmp_path: Path) -> None:
    write_tasks(tmp_path, "tasks.md", ONE_RUNNING)
    write_tasks(tmp_path, "maestro-tasks.md", ONE_RUNNING.replace("TASK-00", "KAP-00"))
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_status_word_in_free_text_is_ignored(tmp_path: Path) -> None:
    """Свободный текст описания (без `|`) не должен читаться как статус.

    Регресс на fix round 1: TASK-002 остаётся TODO, но её описание содержит
    слово «review» вне meta-строки — резолвер обязан вернуть единственную
    реально running-задачу, а не упасть на «больше одной текущей».
    """
    body = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
- Заметка: ждём review этого подхода у ревьюера
"""
    write_tasks(tmp_path, "tasks.md", body)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_status_word_in_free_text_without_running_is_zero(tmp_path: Path) -> None:
    """Слово статуса в прозе без running-задачи не должно выбираться молча.

    Регресс на fix round 1: обе задачи TODO, у TASK-001 в описании фраза
    «уже прошёл review» — резолвер обязан упасть `GateError` («нет
    задачи»), а не тихо вернуть TASK-001.
    """
    body = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | ⬜ TODO
- Заметка: уже прошёл review, ждём мерджа
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
"""
    write_tasks(tmp_path, "tasks.md", body)
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_prose_segment_after_pipe_is_not_status(tmp_path: Path) -> None:
    """Прозаический сегмент после `|` со словом статуса — не статус.

    Регресс на fix round 2: обе задачи TODO, у TASK-002 заметка
    `- Заметка: пример | review нужен от ревьюера` — сегмент после `|`
    содержит слово «review», но это не чистый статус-токен (есть хвост
    «нужен от ревьюера»). Резолвер обязан упасть `GateError` («нет
    задачи»), а не тихо вернуть TASK-002.
    """
    body = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | ⬜ TODO
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
- Заметка: пример | review нужен от ревьюера
"""
    write_tasks(tmp_path, "tasks.md", body)
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_prose_segment_after_pipe_does_not_break_legit_running(tmp_path: Path) -> None:
    """Та же зашумлённая заметка не мешает найти реально running-задачу.

    Симметричный случай к предыдущему тесту: TASK-001 легитимно
    IN_PROGRESS, а у TASK-002 та же зашумлённая заметка
    `- Заметка: пример | review нужен от ревьюера` — резолвер обязан
    вернуть TASK-001, не спутать зашумлённый сегмент со вторым running.
    """
    body = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
- Заметка: пример | review нужен от ревьюера
"""
    write_tasks(tmp_path, "tasks.md", body)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"


def test_dash_bullet_imitation_is_not_status(tmp_path: Path) -> None:
    """Пунктуация-имитация буллета перед словом статуса — не статус.

    Регресс на fix round 3: TASK-001 честно TODO, у TASK-002 единственная
    строка-кандидат — `- Заметка: детали | - REVIEW`, где сегмент после
    `|` — `- REVIEW` (дефис вместо разрешённого emoji). Whitelist-токен
    (`_STATUS_TOKEN_RE`) допускает в префиксе только пятёрку emoji
    spec-runner, поэтому это не статус. Обе задачи без running →
    `GateError` («нет задачи»), а не тихий возврат TASK-002.
    """
    body = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | ⬜ TODO
### TASK-002: Вторая
- Заметка: детали | - REVIEW
"""
    write_tasks(tmp_path, "tasks.md", body)
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_markdown_table_row_is_not_meta_line(tmp_path: Path) -> None:
    """Строка markdown-таблицы в описании — не meta-строка, даже с `|` и статусом.

    Регресс на fix round 3: обе задачи TODO по своей честной meta-строке;
    у TASK-002 ниже — таблица-описание, одна из строк которой
    (`| Модуль A | REVIEW |`) начинается с `|`, а не с буллета `-`/`*`, и
    поэтому не попадает в кандидаты meta-строки (`_META_CANDIDATE_RE`).
    Обе задачи без running → `GateError` («нет задачи»).
    """
    body = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | ⬜ TODO
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
- Таблица модулей:
| Модуль | Статус |
|---|---|
| Модуль A | REVIEW |
"""
    write_tasks(tmp_path, "tasks.md", body)
    with pytest.raises(tdd_gate.GateError):
        tdd_gate.resolve_current_task(tmp_path)


def test_first_candidate_wins_over_noise_below(tmp_path: Path) -> None:
    """Позитивный контроль: реальная meta-строка первая, шум — ниже.

    У TASK-001 честная meta-строка `| 🔄 IN_PROGRESS` идёт сразу после
    заголовка, а ниже — зашумлённый буллет с `|` и таблица со словом
    `REVIEW` в ячейке. Резолвер обязан прочитать статус только из ПЕРВОЙ
    строки-кандидата и вернуть TASK-001, полностью игнорируя шум ниже.
    """
    body = """## Milestone
### TASK-001: Первая
- Приоритет: P1 | 🔄 IN_PROGRESS
- Заметка: пример | review нужен от ревьюера
| Модуль | Статус |
|---|---|
| Модуль A | REVIEW |
### TASK-002: Вторая
- Приоритет: P1 | ⬜ TODO
"""
    write_tasks(tmp_path, "tasks.md", body)
    assert tdd_gate.resolve_current_task(tmp_path) == "TASK-001"
