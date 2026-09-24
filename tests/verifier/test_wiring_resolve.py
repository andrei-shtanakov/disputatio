"""Индекс модулей и правило `construct-only-in` гейта wiring (design §3.2, §3.4).

Деревья синтетические: `Snapshot` собирается прямо из словаря «путь →
текст», git не нужен. Разрешение имён статично и идёт по снимку: вызов
засчитывается, когда множество разрешения его `func` содержит объявленный
класс. Отдельно проверяются циклы реэкспорта (отсечение по активному
стеку, а не по накопленному множеству), независимость запросов разных
вызовов и отказы `WiringInputError` (код 2, §6).
"""

from collections.abc import Mapping

import pytest

from disputatio.verifier.wiring import (
    ConstructRule,
    Violation,
    build_index,
    construct_violations,
)
from disputatio.verifier.wiring_snapshot import Snapshot, WiringInputError

ROOT_FILE = "src/root.py"


def _snapshot(files: Mapping[str, str | bytes]) -> Snapshot:
    """Снимок из словаря «путь → текст»; `bytes` кладутся как есть."""
    encoded = {
        path: text if isinstance(text, bytes) else text.encode("utf-8")
        for path, text in files.items()
    }
    encoded.setdefault(ROOT_FILE, b"")
    return Snapshot(tree="0" * 40, files=encoded)


def _rule(cls: str = "a:C", allowed: tuple[str, ...] = (ROOT_FILE,)) -> ConstructRule:
    module, _, name = cls.partition(":")
    return ConstructRule(id="r", class_module=module, class_name=name, allowed=allowed)


def _sites(
    files: Mapping[str, str | bytes],
    cls: str = "a:C",
    allowed: tuple[str, ...] = (ROOT_FILE,),
) -> list[tuple[str, int]]:
    """Нарушения правила как пары (место, число вызовов)."""
    index = build_index(_snapshot(files), "src")
    return [(v.site, v.count) for v in construct_violations(index, _rule(cls, allowed))]


CLASS_A = "class C:\n    pass\n"


# --- прямые формы вызова -----------------------------------------------------


def test_direct_call_in_defining_module() -> None:
    assert _sites({"src/a.py": CLASS_A + "x = C()\n"}) == [("src/a.py:3", 1)]


def test_violation_record_shape() -> None:
    index = build_index(_snapshot({"src/a.py": CLASS_A + "C()\n"}), "src")
    assert construct_violations(index, _rule()) == [
        Violation(
            rule="r",
            kind="construct-only-in",
            site="src/a.py:3",
            member=None,
            count=1,
        )
    ]


def test_from_import() -> None:
    files = {"src/a.py": CLASS_A, "src/b.py": "from a import C\nC()\n"}
    assert _sites(files) == [("src/b.py:2", 1)]


def test_from_import_as() -> None:
    files = {"src/a.py": CLASS_A, "src/b.py": "from a import C as D\nD()\n"}
    assert _sites(files) == [("src/b.py:2", 1)]


def test_import_module_attribute() -> None:
    files = {"src/a.py": CLASS_A, "src/b.py": "import a\na.C()\n"}
    assert _sites(files) == [("src/b.py:2", 1)]


def test_import_dotted_as() -> None:
    files = {
        "src/a/__init__.py": "",
        "src/a/b.py": CLASS_A,
        "src/u.py": "import a.b as m\nm.C()\n",
    }
    assert _sites(files, cls="a.b:C") == [("src/u.py:2", 1)]


def test_import_dotted_without_as_binds_head() -> None:
    files = {
        "src/a/__init__.py": "",
        "src/a/b.py": CLASS_A,
        "src/u.py": "import a.b\na.b.C()\n",
    }
    assert _sites(files, cls="a.b:C") == [("src/u.py:2", 1)]


def test_reexport_through_package_init() -> None:
    files = {
        "src/pkg/__init__.py": "from pkg.impl import C\n",
        "src/pkg/impl.py": CLASS_A,
        "src/u.py": "from pkg import C\nimport pkg\nC()\npkg.C()\n",
    }
    assert _sites(files, cls="pkg.impl:C") == [("src/u.py:3", 1), ("src/u.py:4", 1)]


def test_reexport_chain() -> None:
    files = {
        "src/pkg/__init__.py": "from .mid import C\n",
        "src/pkg/mid.py": "from .impl import C\n",
        "src/pkg/impl.py": CLASS_A,
        "src/u.py": "from pkg import C\nC()\n",
    }
    assert _sites(files, cls="pkg.impl:C") == [("src/u.py:2", 1)]


@pytest.mark.parametrize(
    "init",
    ["from pkg import impl as alias\n", "import pkg.impl as alias\n"],
)
def test_module_alias_in_package(init: str) -> None:
    files = {
        "src/pkg/__init__.py": init,
        "src/pkg/impl.py": CLASS_A,
        "src/u.py": "import pkg\npkg.alias.C()\n",
    }
    assert _sites(files, cls="pkg.impl:C") == [("src/u.py:2", 1)]


def test_repeated_key_in_one_request_is_not_a_cycle() -> None:
    """Звенья повторно проходят `member(pkg, self)`, но не вложенно."""
    files = {
        "src/pkg/__init__.py": "import pkg as self\nfrom pkg import impl as alias\n",
        "src/pkg/impl.py": CLASS_A,
        "src/u.py": "import pkg\npkg.self.self.alias.C()\n",
    }
    assert _sites(files, cls="pkg.impl:C") == [("src/u.py:2", 1)]


def test_import_inside_function_binds() -> None:
    files = {
        "src/a.py": CLASS_A,
        "src/b.py": "def f():\n    from a import C\n    return C()\n",
    }
    assert _sites(files) == [("src/b.py:3", 1)]


# --- namespace-пакеты ----------------------------------------------------------


def test_namespace_package_import_forms() -> None:
    files = {
        "src/pkg/impl.py": CLASS_A,
        "src/u.py": "import pkg.impl\npkg.impl.C()\n",
        "src/v.py": "from pkg import impl\nimpl.C()\n",
    }
    assert _sites(files, cls="pkg.impl:C") == [("src/u.py:2", 1), ("src/v.py:2", 1)]


def test_namespace_package_any_depth() -> None:
    files = {
        "src/a/b/impl.py": CLASS_A,
        "src/u.py": "import a.b.impl\na.b.impl.C()\n",
        "src/v.py": "from a.b import impl\nimpl.C()\n",
    }
    assert _sites(files, cls="a.b.impl:C") == [("src/u.py:2", 1), ("src/v.py:2", 1)]


def test_relative_imports_inside_namespace_package() -> None:
    files = {
        "src/a/b/impl.py": CLASS_A,
        "src/a/b/u.py": "from .impl import C\nC()\n",
        "src/a/b/v.py": "from . import impl\nimpl.C()\n",
        "src/a/c/w.py": "from ..b.impl import C\nC()\n",
    }
    assert _sites(files, cls="a.b.impl:C") == [
        ("src/a/b/u.py:2", 1),
        ("src/a/b/v.py:2", 1),
        ("src/a/c/w.py:2", 1),
    ]


# --- что нарушением не является -----------------------------------------------


def test_same_short_name_elsewhere_is_not_a_violation() -> None:
    files = {
        "src/a.py": CLASS_A,
        "src/other.py": CLASS_A + "C()\n",
        "src/u.py": "from ext import C\nimport ext\nC()\next.C()\n",
        "src/v.py": "from other import C\nC()\n",
    }
    assert _sites(files) == []


def test_external_star_import_is_ignored() -> None:
    files = {"src/a.py": CLASS_A, "src/u.py": "from ext import *\nC()\n"}
    assert _sites(files) == []


def test_call_in_allowed_file_is_not_a_violation() -> None:
    files = {"src/a.py": CLASS_A, "src/b.py": "from a import C\nC()\n"}
    assert _sites(files, allowed=("src/b.py",)) == []


def test_files_outside_src_are_not_analyzed() -> None:
    files = {
        "src/a.py": CLASS_A,
        "tests/test_a.py": "from a import C\nC()\n",
        "tests/broken.py": "def (:\n",
    }
    assert _sites(files) == []


def test_two_calls_on_one_line_are_one_violation() -> None:
    files = {"src/a.py": CLASS_A, "src/b.py": "from a import C\nx = [C(), C()]\n"}
    assert _sites(files) == [("src/b.py:2", 2)]


def test_inner_call_of_method_chain() -> None:
    files = {"src/a.py": CLASS_A, "src/b.py": "from a import C\nC().method()\n"}
    assert _sites(files) == [("src/b.py:2", 1)]


# --- циклы и независимость запросов --------------------------------------------


def test_reexport_cycle_without_class_terminates() -> None:
    files = {
        "src/a.py": CLASS_A,
        "src/p.py": "from q import X\n",
        "src/q.py": "from p import X\n",
        "src/u.py": "from p import X\nX()\n",
    }
    assert _sites(files) == []


def test_class_reachable_through_other_branch_of_cycle() -> None:
    files = {
        "src/a.py": CLASS_A,
        "src/p.py": "from q import C\nfrom a import C\n",
        "src/q.py": "from p import C\n",
        "src/u.py": "from q import C\nC()\n",
    }
    assert _sites(files) == [("src/u.py:2", 1)]


def test_same_binding_in_two_functions_gives_two_violations() -> None:
    """Стек обхода не копится между запросами разных вызовов."""
    files = {
        "src/a.py": CLASS_A,
        "src/b.py": (
            "from a import C\ndef f():\n    return C()\ndef g():\n    return C()\n"
        ),
    }
    assert _sites(files) == [("src/b.py:3", 1), ("src/b.py:5", 1)]


# --- отказы: код 2 -------------------------------------------------------------


def test_class_module_missing() -> None:
    index = build_index(_snapshot({"src/a.py": CLASS_A}), "src")
    with pytest.raises(WiringInputError, match="nope"):
        construct_violations(index, _rule(cls="nope:C"))


@pytest.mark.parametrize(
    "text",
    [
        "class Outer:\n    class C:\n        pass\n",
        "def f():\n    class C:\n        pass\n",
        "C = 1\n",
    ],
)
def test_class_not_top_level(text: str) -> None:
    index = build_index(_snapshot({"src/a.py": text}), "src")
    with pytest.raises(WiringInputError, match="C"):
        construct_violations(index, _rule())


def test_namespace_package_is_not_a_class_module() -> None:
    index = build_index(_snapshot({"src/pkg/impl.py": CLASS_A}), "src")
    with pytest.raises(WiringInputError):
        construct_violations(index, _rule(cls="pkg:C"))


def test_allowed_path_missing() -> None:
    index = build_index(_snapshot({"src/a.py": CLASS_A}), "src")
    with pytest.raises(WiringInputError, match="src/gone.py"):
        construct_violations(index, _rule(allowed=(ROOT_FILE, "src/gone.py")))


def test_allowed_path_outside_src_is_missing() -> None:
    files = {"src/a.py": CLASS_A, "tests/t.py": ""}
    index = build_index(_snapshot(files), "src")
    with pytest.raises(WiringInputError, match="tests/t.py"):
        construct_violations(index, _rule(allowed=("tests/t.py",)))


def test_star_import_of_snapshot_module() -> None:
    files = {"src/a.py": CLASS_A, "src/u.py": "from a import *\n"}
    with pytest.raises(WiringInputError, match="src/u.py"):
        build_index(_snapshot(files), "src")


def test_relative_star_import_of_snapshot_module() -> None:
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/a.py": CLASS_A,
        "src/pkg/u.py": "def f():\n    from .a import *\n",
    }
    with pytest.raises(WiringInputError, match="src/pkg/u.py"):
        build_index(_snapshot(files), "src")


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("src/top.py", "from . import x\n"),
        ("src/pkg/__init__.py", "from .. import x\n"),
        ("src/pkg/m.py", "from ..other import x\n"),
    ],
)
def test_relative_import_escaping_src(path: str, text: str) -> None:
    with pytest.raises(WiringInputError, match=path):
        build_index(_snapshot({path: text}), "src")


def test_unparsable_file() -> None:
    with pytest.raises(WiringInputError, match="src/bad.py"):
        build_index(_snapshot({"src/bad.py": "def (:\n"}), "src")


def test_undecodable_file() -> None:
    files: Mapping[str, str | bytes] = {"src/bad.py": b"x = '\xff\xfe'\n"}
    with pytest.raises(WiringInputError, match="src/bad.py"):
        build_index(_snapshot(files), "src")


def test_pep263_cookie_is_honored() -> None:
    text = "# -*- coding: latin-1 -*-\nfrom a import C\ns = '\xe9'\nC()\n"
    files: Mapping[str, str | bytes] = {
        "src/a.py": CLASS_A,
        "src/b.py": text.encode("latin-1"),
    }
    assert _sites(files) == [("src/b.py:4", 1)]


# --- коллизия модульных имён ---------------------------------------------------


@pytest.mark.parametrize(
    "paths",
    [
        ("src/pkg.py", "src/pkg/__init__.py"),
        ("src/x.y.py", "src/x/y.py"),
    ],
)
def test_module_name_collision_is_input_error(paths: tuple[str, str]) -> None:
    """Два файла с одним модульным именем — код 2, а не молча один из них."""
    files: dict[str, str | bytes] = {"src/a.py": CLASS_A}
    files.update({path: "from a import C\nC()\n" for path in paths})
    with pytest.raises(WiringInputError) as excinfo:
        build_index(_snapshot(files), "src")
    for path in paths:
        assert path in str(excinfo.value)


def test_module_shadowing_namespace_package_is_still_scanned() -> None:
    """`src/pkg.py` рядом с каталогом `src/pkg/` без `__init__.py`.

    В Python обычный модуль побеждает namespace-пакет, коллизии файлов нет:
    оба файла — разные модули снимка, и оба сканируются.
    """
    files = {
        "src/a.py": CLASS_A,
        "src/pkg.py": "from a import C\nC()\n",
        "src/pkg/impl.py": "from a import C\nC()\n",
    }
    assert _sites(files) == [("src/pkg.py:2", 1), ("src/pkg/impl.py:2", 1)]
