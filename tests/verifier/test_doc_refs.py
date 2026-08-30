"""Тесты парсера ссылок `doc_refs` (TASK-007, §6 SPEC-002).

Импорт `disputatio.verifier.doc_refs` — внутри тестов: на red-фазе модуля
ещё нет, и импорт на уровне файла сломал бы collection (конвенция —
`test_gate_execution.py`).
"""

from types import ModuleType


def _import_doc_refs() -> ModuleType:
    try:
        from disputatio.verifier import doc_refs
    except ImportError as exc:  # red-фаза: doc_refs.py ещё не создан
        raise AssertionError(
            "src/disputatio/verifier/doc_refs.py ещё не создан"
        ) from exc
    return doc_refs


# ---------------------------------------------------------------------------
# parse_doc_refs — распознаваемые формы
# ---------------------------------------------------------------------------


def test_inline_markdown_link_is_recognized() -> None:
    doc_refs = _import_doc_refs()
    text = "См. [спеку](docs/specs/foo.md) для деталей.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    ref = refs[0]
    assert ref.kind == "md_link"
    assert ref.target == "docs/specs/foo.md"
    assert ref.line == 1
    assert ref.anchor is None


def test_inline_markdown_link_with_anchor_splits_target_and_anchor() -> None:
    doc_refs = _import_doc_refs()
    text = "[раздел](docs/specs/foo.md#some-section)\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].target == "docs/specs/foo.md"
    assert refs[0].anchor == "some-section"


def test_same_document_anchor_link_has_empty_target() -> None:
    doc_refs = _import_doc_refs()
    text = "[выше](#overview)\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].kind == "md_link"
    assert refs[0].target == ""
    assert refs[0].anchor == "overview"


def test_reference_style_link_resolves_via_definition() -> None:
    doc_refs = _import_doc_refs()
    text = (
        "Смотри [план][plan-ref] за подробностями.\n\n[plan-ref]: docs/plans/foo.md\n"
    )

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    ref = refs[0]
    assert ref.kind == "md_link"
    assert ref.target == "docs/plans/foo.md"
    assert ref.line == 1  # строка использования, не строка определения


def test_reference_style_collapsed_form_uses_text_as_label() -> None:
    doc_refs = _import_doc_refs()
    text = "Смотри [plan-ref][] за подробностями.\n\n[plan-ref]: docs/plans/foo.md\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].target == "docs/plans/foo.md"


def test_reference_style_link_without_definition_is_not_recognized() -> None:
    """Ссылка без определения — цель не выводима, DocRef не порождается."""
    doc_refs = _import_doc_refs()
    text = "Смотри [план][no-such-ref] за подробностями.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert refs == []


def test_unresolved_reference_link_is_reported_separately() -> None:
    """Цель не выводима — но сама форма увидена и названа (задача 7, фикс).

    `DocRef` такая ссылка по-прежнему не порождает: резолвить нечего. Но
    молчать о ней парсер не вправе — `doc-links` baseline-гейт, и `pass`
    по документу с битой reference-ссылкой означал бы «проверено», хотя
    проверена была не вся форма.
    """
    doc_refs = _import_doc_refs()
    text = "Смотри [план][no-such-ref] за подробностями.\n"

    parsed = doc_refs.parse_document(text)

    assert parsed.refs == []
    assert [(item.label, item.line) for item in parsed.unresolved] == [
        ("no-such-ref", 1)
    ]


def test_resolved_reference_link_leaves_nothing_unresolved() -> None:
    """Определение найдено — форма разрешена, о ней сообщать нечего."""
    doc_refs = _import_doc_refs()
    text = "Смотри [план][plan-ref].\n\n[plan-ref]: docs/plans/foo.md\n"

    parsed = doc_refs.parse_document(text)

    assert parsed.unresolved == []
    assert [ref.target for ref in parsed.refs] == ["docs/plans/foo.md"]


def test_bare_bracket_label_is_not_unresolved_either() -> None:
    """`[DESIGN-002]` не ссылка вовсе — и в неразрешённые не попадает.

    Иначе новый `warning` сыпался бы на каждой метке трассируемости, а
    ровно этой эвристики §6 и запрещает: shortcut-ссылку от метки не
    отличить, поэтому обе формы парсер не признаёт ссылкой изначально.
    """
    doc_refs = _import_doc_refs()

    parsed = doc_refs.parse_document("Требование [REQ-004] описано ниже.\n")

    assert parsed.refs == []
    assert parsed.unresolved == []


def test_bare_bracket_label_is_not_a_reference_link() -> None:
    """`[DESIGN-002]` — трассируемостная метка, не shortcut-ссылка."""
    doc_refs = _import_doc_refs()
    text = "Схема `verification.json` ([DESIGN-004], [REQ-004]).\n"

    refs = doc_refs.parse_doc_refs(text)

    assert refs == []


def test_autolink_to_local_path_is_recognized() -> None:
    doc_refs = _import_doc_refs()
    text = "См. <src/disputatio/verifier/doc_refs.py> целиком.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].kind == "autolink"
    assert refs[0].target == "src/disputatio/verifier/doc_refs.py"


def test_autolink_with_uri_scheme_is_out_of_scope() -> None:
    """URL-автоссылка — не про пути репозитория, DocRef не порождается."""
    doc_refs = _import_doc_refs()
    text = "Подробнее: <https://example.com/docs>.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert refs == []


def test_bare_html_like_autolink_without_path_shape_is_not_recognized() -> None:
    doc_refs = _import_doc_refs()
    text = "<br> и <Foo> не являются путями.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert refs == []


def test_inline_code_path_is_recognized() -> None:
    doc_refs = _import_doc_refs()
    text = "Модуль `src/disputatio/runtime/pipeline_runner.py` ещё не написан.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].kind == "code_path"
    assert refs[0].target == "src/disputatio/runtime/pipeline_runner.py"


def test_inline_code_line_ref_is_recognized() -> None:
    doc_refs = _import_doc_refs()
    text = "См. `src/disputatio/verifier/doc_gates.py:42` за реализацией.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].kind == "code_line_ref"
    assert refs[0].target == "src/disputatio/verifier/doc_gates.py:42"


def test_code_line_ref_with_matching_trailing_quote_sets_expected_text() -> None:
    doc_refs = _import_doc_refs()
    text = "См. `src/mod.py:2` («b = 2») за деталями.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].kind == "code_line_ref"
    assert refs[0].expected_text == "b = 2"


# ---------------------------------------------------------------------------
# expected_text — форма фиксирована однозначно (фикс-раунд 1, Important-2):
# обманные формы обязаны давать `expected_text is None`, а не угаданное
# значение (fail-closed на распознавании, не на сравнении).
# ---------------------------------------------------------------------------


def test_code_line_ref_with_comma_before_quote_is_not_expected_text() -> None:
    """Запятая между спаном и скобкой — не часть зафиксированной формы."""
    doc_refs = _import_doc_refs()
    text = "См. `src/mod.py:2`, («b = 2»).\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].expected_text is None


def test_code_line_ref_with_quote_on_next_line_is_not_expected_text() -> None:
    """Кавычки на следующей строке — форма привязана к той же строке."""
    doc_refs = _import_doc_refs()
    text = "См. `src/mod.py:2`\n(«b = 2»).\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].expected_text is None


def test_code_line_ref_with_straight_quotes_is_not_expected_text() -> None:
    """Прямые кавычки вместо «ёлочек» — не распознаются эвристически."""
    doc_refs = _import_doc_refs()
    text = 'См. `src/mod.py:2` ("b = 2").\n'

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].expected_text is None


def test_inline_code_without_slash_is_not_a_path() -> None:
    """`pytest`, `v1.2.3` — не пути: нет `/`, распознавание не эвристическое."""
    doc_refs = _import_doc_refs()
    text = "Запускается `pytest`, версия схемы `v1.2.3`.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert refs == []


def test_path_like_prose_outside_any_form_is_not_recognized() -> None:
    """Текст «похожий на путь» вне ссылки/inline-code — не DocRef вовсе."""
    doc_refs = _import_doc_refs()
    text = "Модуль src/disputatio/runtime/pipeline_runner.py ещё не написан.\n"

    refs = doc_refs.parse_doc_refs(text)

    assert refs == []


# ---------------------------------------------------------------------------
# Файлы: Modify:/Test:/Create: — declared_existing / declared_planned
# ---------------------------------------------------------------------------


def test_modify_bullet_is_declared_existing() -> None:
    doc_refs = _import_doc_refs()
    text = "**Файлы:**\n- Modify: `src/disputatio/verifier/runner.py`\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].kind == "declared_existing"
    assert refs[0].target == "src/disputatio/verifier/runner.py"
    assert refs[0].line == 2


def test_test_bullet_is_declared_existing() -> None:
    doc_refs = _import_doc_refs()
    text = "- Test: `tests/verifier/test_doc_refs.py`\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].kind == "declared_existing"


def test_create_bullet_is_declared_planned() -> None:
    doc_refs = _import_doc_refs()
    text = "- Create: `src/disputatio/verifier/doc_refs.py`\n"

    refs = doc_refs.parse_doc_refs(text)

    assert len(refs) == 1
    assert refs[0].kind == "declared_planned"
    assert refs[0].target == "src/disputatio/verifier/doc_refs.py"


def test_wrapped_bullet_across_lines_captures_every_backtick_path() -> None:
    """Эталонный кейс: бриф задачи 7 сам оформлен так же (перенос строки)."""
    doc_refs = _import_doc_refs()
    text = (
        "**Файлы:**\n"
        "- Create: `src/disputatio/verifier/doc_refs.py` (парсер распознаваемых\n"
        "  форм), `src/disputatio/verifier/doc_gates.py` (гейты 1-3)\n"
        "- Test: `tests/verifier/test_doc_refs.py`,"
        " `tests/verifier/test_doc_gates_links.py`\n"
    )

    refs = doc_refs.parse_doc_refs(text)

    planned = [r for r in refs if r.kind == "declared_planned"]
    existing = [r for r in refs if r.kind == "declared_existing"]
    assert {r.target for r in planned} == {
        "src/disputatio/verifier/doc_refs.py",
        "src/disputatio/verifier/doc_gates.py",
    }
    assert {r.target for r in existing} == {
        "tests/verifier/test_doc_refs.py",
        "tests/verifier/test_doc_gates_links.py",
    }
    # Второй путь Create: обязан приписаться ко второй физической строке.
    second_planned = next(
        r for r in planned if r.target == "src/disputatio/verifier/doc_gates.py"
    )
    assert second_planned.line == 3


# ---------------------------------------------------------------------------
# Экранирование: обратный слеш оставляет форму обычным текстом
# ---------------------------------------------------------------------------


def test_escaped_reference_link_is_plain_text() -> None:
    r"""`\[plan][missing]` — литерал, а не битая ссылка (§6).

    Ложная запись `unresolved_ref` уходит в детерминированный отчёт
    `doc-links`, то есть прямо к doc-ревьюеру: гейт при этом остаётся
    `PASS`, поэтому шум ничем не гасится и выглядит находкой.
    """
    doc_refs = _import_doc_refs()

    parsed = doc_refs.parse_document("Форма записывается так: \\[plan][missing].\n")

    assert parsed.refs == []
    assert parsed.unresolved == []


def test_escaped_inline_link_is_plain_text() -> None:
    r"""`\[текст](docs/missing.md)` — тоже литерал: дыра была общей для форм."""
    doc_refs = _import_doc_refs()

    refs = doc_refs.parse_doc_refs("Пишется \\[текст](docs/missing.md) вот так.\n")

    assert refs == []


def test_escaped_autolink_is_plain_text() -> None:
    r"""`\<docs/missing.md>` — литерал, а не автоссылка."""
    doc_refs = _import_doc_refs()

    refs = doc_refs.parse_doc_refs("Автоссылка пишется \\<docs/missing.md>.\n")

    assert refs == []


def test_escaped_code_span_is_plain_text() -> None:
    r"""Экранированный бэктик спана не открывает — пути внутри нет.

    Экранирован ровно ОДИН бэктик, открывающий: пара `` \`x` `` — та форма,
    на которой видно правило. Экранируй тест оба, и он проходил бы даром —
    хвостовой слеш попадает в содержимое и ломает шаблон пути сам по себе.

    Цена ошибки здесь выше, чем у reference-формы: несуществующий путь в
    таком «спане» — не `warning`, а `fail` гейта `doc-paths`.
    """
    doc_refs = _import_doc_refs()

    refs = doc_refs.parse_doc_refs("Спан пишется \\`docs/missing.md` вот так.\n")

    assert refs == []


def test_escaped_backtick_in_a_declaration_bullet_is_plain_text() -> None:
    r"""Правило одно на все формы, включая пути деклараций `Modify:`."""
    doc_refs = _import_doc_refs()

    refs = doc_refs.parse_doc_refs("- Modify: \\`docs/missing.md\\`\n")

    assert refs == []


def test_escaped_backslash_leaves_the_link_alone() -> None:
    r"""`\\[текст](docs/plan.md)` — экранирован слеш, а не скобка.

    Правило — чётность: считается не «есть ли слеш перед формой», а сколько
    их. Иначе литерал `\` перед настоящей ссылкой скрывал бы её от гейтов.
    """
    doc_refs = _import_doc_refs()

    refs = doc_refs.parse_doc_refs("Слеш \\\\[текст](docs/plan.md) и ссылка.\n")

    assert [(ref.kind, ref.target) for ref in refs] == [("md_link", "docs/plan.md")]


# ---------------------------------------------------------------------------
# github_slug — нормализация якорей
# ---------------------------------------------------------------------------


def test_github_slug_casefolds_and_hyphenates_spaces() -> None:
    doc_refs = _import_doc_refs()

    assert doc_refs.github_slug("Hello World", {}) == "hello-world"


def test_github_slug_strips_punctuation_but_keeps_hyphens() -> None:
    doc_refs = _import_doc_refs()

    assert doc_refs.github_slug("Doc-Gates (v1)!", {}) == "doc-gates-v1"


def test_github_slug_keeps_unicode_letters_as_is() -> None:
    doc_refs = _import_doc_refs()

    assert doc_refs.github_slug("Привет Мир", {}) == "привет-мир"


def test_github_slug_percent_decodes_input() -> None:
    doc_refs = _import_doc_refs()

    assert doc_refs.github_slug("Hello%20World", {}) == "hello-world"


def test_github_slug_appends_numeric_suffix_for_repeats() -> None:
    doc_refs = _import_doc_refs()
    seen: dict[str, int] = {}

    first = doc_refs.github_slug("Overview", seen)
    second = doc_refs.github_slug("Overview", seen)
    third = doc_refs.github_slug("Overview", seen)

    assert (first, second, third) == ("overview", "overview-1", "overview-2")
