"""Узость allowlist'а модулей в скане [REQ-010] (review-фикс TASK-010).

`test_no_agent_cli.py` пинит запрет сторонних импортов на `pydantic`/`httpx`,
но не пинит границу внутри самого `disputatio`. Между тем именно она несёт
инвариант: `disputatio.adapters` — пакет, который агентские CLI и запускает,
и его импорт из Verifier'а обходит INV-10 целиком, не оставив в исходниках ни
одного токена вроде `claude`. Без этих тестов расширение
`ALLOWED_MODULE_PREFIXES` до `{"disputatio"}` — как и сравнение префикса без
точки-разделителя — проходит suite незамеченным.

Тесты read-only: сканируются строки-исходники, ничего не импортируется.
"""

from .agent_cli_scanner import scan_source


def test_scan_source_flags_sibling_disputatio_packages() -> None:
    """Соседние пакеты `disputatio` вне allowlist'а — нарушение, как и чужие."""
    source = "import disputatio.adapters\nfrom disputatio.orchestrator import loop\n"

    assert scan_source(source) == ["disputatio.adapters", "disputatio.orchestrator"]


def test_scan_source_flags_modules_that_only_prefix_match_allowlist() -> None:
    """Совпадение префикса без разделителя не разрешение: граница по точке."""
    source = "import disputatio.contracts_evil\nfrom disputatio.verifierx import shim\n"

    assert scan_source(source) == ["disputatio.contracts_evil", "disputatio.verifierx"]
