"""Скан исходников `disputatio.verifier` на агентские CLI ([REQ-010], TASK-010).

Не тест, а инструмент теста `test_no_agent_cli.py`: файл намеренно назван без
префикса `test_`, чтобы pytest не собирал его как модуль с тестами. Живёт под
`tests/`, поэтому ему, в отличие от самого пакета, разрешён файловый I/O — но
только read-only чтение исходников для `ast.parse`.

Две независимые проверки одного инварианта INV-10:

* текстовая — упоминание агентского CLI в любом виде: в argv, строке,
  комментарии или docstring'е (регистр не важен);
* по AST — импорт вне stdlib и `disputatio.contracts`/`disputatio.verifier`,
  плюс динамический импорт (`__import__`, `importlib.import_module`), который
  обошёл бы allowlist по имени модуля.

Гарантия статическая и потому неполная: имя, склеенное из кусков
(`"cla" + "ude"`), взятое из переменной окружения или прочитанное из файла,
текстовым сканом не ловится. Это осознанный предел — скан защищает от
случайного протаскивания агентского вызова в пакет, а не от намеренной
маскировки. Команда из `GateSpec.cmd` под скан не попадает вовсе и попадать не
должна: исполнять команды конфигурации gates — прямая обязанность Verifier'а.
"""

import ast
import sys
from pathlib import Path

import disputatio.verifier as _verifier_package

# Агентские CLI и SDK: `claude`/`codex` названы в [REQ-010] поимённо, остальные
# — тот же класс инструментов («и т.п.»). Токен ищется как подстрока, поэтому
# `claude-code`/`claude_code`/`CLAUDE` попадают в один `claude`. `cursor` взят
# только в форме `cursor-agent`: голое слово означало бы курсор чтения и давало
# бы ложные срабатывания на честном коде.
FORBIDDEN_CLI_TOKENS: frozenset[str] = frozenset(
    {
        "aider",
        "anthropic",
        "claude",
        "codex",
        "copilot",
        "cursor-agent",
        "gemini",
        "litellm",
        "ollama",
        "openai",
    }
)

# Пакеты `disputatio`, на которые Verifier вправе опираться ([REQ-010]):
# контракты артефактов и собственные модули. Список именно такой узкий, а не
# префикс `disputatio`: соседние пакеты — в первую очередь `disputatio.adapters`,
# который агентские CLI как раз и запускает, — Verifier'у недоступны, и импорт
# любого из них обязан считаться нарушением.
ALLOWED_MODULE_PREFIXES: frozenset[str] = frozenset(
    {"disputatio.contracts", "disputatio.verifier"}
)

# Динамический импорт: имя модуля становится значением времени выполнения, и
# allowlist по AST перестаёт что-либо гарантировать.
FORBIDDEN_IMPORT_CALLS: frozenset[str] = frozenset({"__import__", "import_module"})


def verifier_package_dir() -> Path:
    """Каталог пакета `disputatio.verifier` — точка входа динамического обхода."""
    return Path(_verifier_package.__file__).resolve().parent


def iter_module_paths(package_dir: Path) -> list[Path]:
    """Все `*.py`-файлы `package_dir` рекурсивно, отсортированные по пути.

    Обход файловой системы, а не статический список имён: новый модуль — и
    целый новый под-пакет — попадает в проверку без правки сканера.
    """
    return sorted(package_dir.rglob("*.py"))


def _is_allowed_module(module_name: str) -> bool:
    """`True` для stdlib-модуля и для модулей allowlist'а `disputatio`.

    Критерий stdlib — `sys.stdlib_module_names`, а не «пакет установлен»:
    `pydantic` и `pyyaml` из зависимостей проекта Verifier'у не разрешены, как
    и любая другая сторонняя библиотека ([REQ-010]).
    """
    top_level = module_name.split(".", 1)[0]
    if top_level in sys.stdlib_module_names:
        return True
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in ALLOWED_MODULE_PREFIXES
    )


def find_disallowed_imports(tree: ast.AST) -> list[str]:
    """Импорты дерева `tree` вне allowlist'а, по имени модуля/импорта.

    Относительный импорт (`from . import x`) — всегда нарушение: имя цели в нём
    неполное, и allowlist по имени о ней ничего сказать не может. Пакет
    пользуется только абсолютными импортами, так что ложных срабатываний это не
    даёт.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(
                alias.name for alias in node.names if not _is_allowed_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = f"{'.' * node.level}{node.module or ''}"
            if node.level == 0 and _is_allowed_module(module):
                continue
            if node.module is None:  # `from . import x` — module пуст, есть alias'ы
                violations.extend(
                    f"{'.' * node.level}{alias.name}" for alias in node.names
                )
            else:
                violations.append(module)
    return violations


def find_dynamic_imports(tree: ast.AST) -> list[str]:
    """Вызовы `__import__(...)` и `importlib.import_module(...)` в дереве `tree`."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_IMPORT_CALLS:
            violations.append(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_IMPORT_CALLS:
            violations.append(func.attr)
    return violations


def find_agent_cli_mentions(source: str) -> list[str]:
    """Токены агентских CLI, встреченные в тексте `source`, в алфавитном порядке.

    Скан идёт по сырому тексту, а не по AST: упоминание в комментарии или в
    docstring'е — такой же сигнал, как argv, а комментарии в AST не попадают.
    Порядок фиксирован сортировкой — `frozenset` сам по себе неупорядочен.
    """
    lowered = source.lower()
    return [token for token in sorted(FORBIDDEN_CLI_TOKENS) if token in lowered]


def scan_source(source: str) -> list[str]:
    """Все нарушения исходника `source`: импорты, динамические импорты, упоминания."""
    tree = ast.parse(source)
    return [
        *find_disallowed_imports(tree),
        *find_dynamic_imports(tree),
        *find_agent_cli_mentions(source),
    ]


def check_no_agent_cli(package_dir: Path) -> dict[str, list[str]]:
    """Нарушения по модулям `package_dir`; пустой `dict` — чисто ([REQ-010]).

    Ключ — путь модуля относительно `package_dir` в posix-форме: имени файла не
    хватило бы, одноимённые модули разных под-пакетов затирали бы друг друга.
    """
    violations: dict[str, list[str]] = {}
    for path in iter_module_paths(package_dir):
        found = scan_source(path.read_text(encoding="utf-8"))
        if found:
            violations[path.relative_to(package_dir).as_posix()] = found
    return violations
