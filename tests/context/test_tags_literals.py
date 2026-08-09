"""Review-fix TASK-001: метки — литералы, а не значение, вычисленное на импорте.

`test_tags.py` пинит детерминизм только внутри одного процесса: два вызова
`wrap_artifact_data` подряд дают одинаковые байты. Этого мало. Мутант

    _OPEN_TAG = f"<<<ARTIFACT_DATA:{uuid.uuid4()} …>>>"

вычисляется один раз на импорте, поэтому внутри процесса он константа —
и весь `tests/context/` на нём зелёный (проверено экспериментом). При этом
он нарушает и [ADR-001] («метки фиксированы литералами»), и NFR-002: у двух
запусков оркестратора промпты разъезжаются побайтово, ломая сравнение
раундов, кэш `--resume` адаптера и воспроизводимость отчётов.

Дыра закрывается с двух сторон:

* структурно — разбором AST модуля: каждая константа, попадающая в
  выходные байты, должна быть присвоена строковым литералом верхнего
  уровня (ни `f`-строки, ни вызова, ни имени);
* поведенчески — сравнением байтов из двух ОТДЕЛЬНЫХ процессов с
  результатом в текущем: именно это и требует NFR-002.

Модуль отдельный, а не дописан в `test_tags_fidelity.py`, чтобы тема
(«происхождение меток») не смешивалась с темой того файла («дословность
содержимого»).
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from disputatio.context import tags

#: Константы, чьи значения попадают в результат `wrap_artifact_data`.
EMITTED_CONSTANTS = ("_OPEN_TAG", "_CLOSE_TAG", "_ZWSP")

_PROBE = (
    "import sys;"
    "from disputatio.context.tags import wrap_artifact_data;"
    "sys.stdout.buffer.write(wrap_artifact_data('данные').encode())"
)


def _module_level_string_literals() -> dict[str, str]:
    """Имена верхнего уровня `tags.py`, присвоенные строковым литералом.

    Fail-closed: присваивание через `f`-строку, вызов или имя в результат
    не попадает вовсе, и тест падает на проверке наличия имени.
    """
    source = Path(tags.__file__).read_text(encoding="utf-8")
    literals: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                literals[target.id] = node.value.value
    return literals


@pytest.mark.parametrize("name", EMITTED_CONSTANTS)
def test_emitted_constant_is_a_plain_string_literal(name: str) -> None:
    """[ADR-001]: метки не генерируются — ни на вызов, ни на импорт."""
    literals = _module_level_string_literals()

    assert name in literals, (
        f"{name} в tags.py присвоен не строковым литералом верхнего уровня — "
        "значение вычисляется, промпт перестаёт быть воспроизводимым"
    )
    assert literals[name] == getattr(tags, name), (
        f"{name} во время выполнения не равен литералу из исходника"
    )


def test_wrap_is_byte_identical_across_processes() -> None:
    """NFR-002: воспроизводимость держится между запусками, а не внутри одного."""
    runs = [
        subprocess.run(
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            check=True,
        ).stdout
        for _ in range(2)
    ]

    assert runs[0] == runs[1]
    assert runs[0] == tags.wrap_artifact_data("данные").encode()
