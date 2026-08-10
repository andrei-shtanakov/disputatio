"""Review-fix TASK-007: NFR-002 держится МЕЖДУ процессами, а не внутри одного.

`test_hygiene.py` сравнивает два вызова `build_author_prompt` в одном
процессе — ровно так же, как `test_tags.py` сравнивал два вызова
`wrap_artifact_data`, и ровно так же этого мало (см. `test_tags_literals.py`).
Реализация, достающая тексты замечаний обходом множества id,

    open_issues = [by_id[k] for k in set(decision.open_issues_carried)]

внутри процесса даёт один и тот же порядок при любом порядке аргументов:
весь `tests/context/` на ней зелёный, включая тест «порядок построения
входов не важен» (проверено экспериментом — дайджест промпта менялся на
каждом из четырёх сидов). Между запусками оркестратора порядок секций
разъезжается вместе с хэш-сидом, а с ним и байты промпта: ломается
сравнение раундов, кэш `--resume` адаптера и воспроизводимость отчётов.

Проверка поведенческая и на собранном промпте целиком: `prompt_probe.py`
считает дайджест в отдельных процессах с разными сидами, а сравниваются они
с дайджестом текущего процесса — у которого сид свой.
"""

import os
import subprocess
import sys

import pytest

from . import prompt_probe

#: Сиды разные и ненулевые: `PYTHONHASHSEED=0` ВЫКЛЮЧАЕТ рандомизацию, и на
#: одних нулях порядок обхода `set` совпал бы у всех процессов сам собой.
HASH_SEEDS = ("1", "2", "3", "5", "8")


def _digests_in_subprocess(seed: str) -> list[str]:
    """Дайджесты обоих промптов из отдельного процесса с сидом `seed`.

    Скрипт запускается по пути файла, а не через `-m`: `prompt_probe` лежит
    в тестовом пакете, чей корень на `sys.path` кладёт pytest, и в чистом
    подпроцессе такого импорта нет.
    """
    result = subprocess.run(
        [sys.executable, prompt_probe.__file__],
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    )
    return result.stdout.decode().split()


@pytest.mark.parametrize("seed", HASH_SEEDS)
def test_prompts_are_byte_identical_across_processes(seed: str) -> None:
    """NFR-002: чужой хэш-сид не меняет в собранных промптах ни байта."""
    expected = prompt_probe.digests()

    assert len(expected) == 2, "эталон обязан давать оба промпта"
    assert _digests_in_subprocess(seed) == expected, (
        f"промпты разъехались при PYTHONHASHSEED={seed}: порядок секций "
        "зависит от обхода неупорядоченной коллекции"
    )


def test_probe_carries_both_issue_sections() -> None:
    """Эталон не вырожден: обе секции замечаний непусты, иначе сравнивать нечего.

    Без этого мутант, выбросивший секцию замечаний целиком, оставил бы
    сравнение дайджестов зелёным — сравнивать одинаково пустое легко.
    """
    author_prompt, reviewer_prompt = prompt_probe.build_prompts()
    carried = set(prompt_probe.CARRIED)
    resolved = set(prompt_probe.ISSUE_IDS) - carried

    assert carried and resolved, "нужны и открытые, и заявленные решёнными"
    for issue_id in carried:
        assert issue_id in author_prompt, f"открытое {issue_id} не дошло до автора"
        assert issue_id not in reviewer_prompt
    for issue_id in resolved:
        assert issue_id in reviewer_prompt, f"решённое {issue_id} не дошло к ревьюеру"
        assert issue_id not in author_prompt
