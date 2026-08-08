"""D0 smoke: пакет импортируется, версия соответствует ожидаемой константе теста."""

from disputatio import __version__


def test_package_importable() -> None:
    assert __version__ == "0.1.0"
