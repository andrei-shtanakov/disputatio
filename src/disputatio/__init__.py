"""disputatio: headless-оркестратор author↔reviewer debate loop (SPEC-001).

Корневой пакет намеренно не реэкспортирует ни `runtime`, ни `cli`: `import
disputatio` не должен тянуть за собой `anyio`, адаптеры и subprocess
[DESIGN-022]. Потребители импортируют подсистемы явно — `from
disputatio.runtime import ...`.

Версия — литерал, а не `importlib.metadata`: так рассинхрон с
`project.version` в `pyproject.toml` ловится тестом статически и не зависит
от того, установлен ли пакет.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
