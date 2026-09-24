"""Markdown-сторона гейта wiring: `MarkdownPlanReader` (design §5.3).

Гейт видит в плане ровно то, что видит читатель отрендеренного документа:
разбор делает CommonMark-парсер (`markdown-it-py`, пресет `commonmark`), а
`MarkdownPlanReader` лишь выбирает токены — заголовки задач верхнего уровня,
границы разделов и видимый текст (§5.3). Тесты гоняют настоящий парсер:
подделка семантики Markdown проверяла бы саму себя.
"""

import pytest

from disputatio.plan_markdown import MarkdownPlanReader
from disputatio.verifier.wiring import parse_cover, parse_rules
from disputatio.verifier.wiring_snapshot import WiringInputError

READER = MarkdownPlanReader()

EXAMPLE_RULES_BODY = """\
[[rule]]
id = "p10-policy"
kind = "construct-only-in"
class = "pkg.mod:C"
allowed = ["src/pkg/mod.py"]
"""


class TestTaskSections:
    def test_recognizes_both_heading_styles(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "текст первой задачи\n"
            "### Task 2: покрытие\n"
            "текст второй задачи\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1, 2}
        assert "текст первой задачи" in sections[1]
        assert "текст второй задачи" in sections[2]

    def test_section_ends_at_next_heading_level_1_to_3(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "тело задачи 1\n"
            "## Другой раздел плана\n"
            "тело задачи 1 сюда уже не входит\n"
        )

        sections = READER.task_sections(text)

        assert "тело задачи 1" in sections[1]
        assert "тело задачи 1 сюда уже не входит" not in sections[1]

    def test_level_4_heading_does_not_end_section(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "тело задачи 1\n"
            "#### Подраздел\n"
            "всё ещё тело задачи 1\n"
        )

        sections = READER.task_sections(text)

        assert "всё ещё тело задачи 1" in sections[1]

    def test_level_2_heading_is_not_task_heading_but_ends_section(self) -> None:
        """`## Task N:` не заголовок задачи (§5.3: только `###`), но обрывает раздел."""
        text = (
            "### Задача 1: разбор\n"
            "тело задачи 1\n"
            "## Task 3: не заголовок задачи\n"
            "текст после заголовка второго уровня\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "тело задачи 1" in sections[1]
        assert "текст после заголовка второго уровня" not in sections[1]

    def test_level_1_heading_is_not_task_heading_but_ends_section(self) -> None:
        """`# Задача N:` — не заголовок задачи (только `###`), но обрывает раздел."""
        text = (
            "### Задача 1: разбор\n"
            "тело задачи 1\n"
            "# Задача 3: не заголовок задачи\n"
            "текст после заголовка первого уровня\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "тело задачи 1" in sections[1]
        assert "текст после заголовка первого уровня" not in sections[1]

    def test_non_level_3_task_like_heading_does_not_trigger_duplicate(self) -> None:
        """`## Task 3:` рядом с настоящей `### Задача 3:` — не дубль номера."""
        text = (
            "### Задача 3: настоящая\n"
            "текст настоящей задачи 3\n"
            "## Task 3: похоже на задачу, но не она\n"
            "текст после\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {3}
        assert "текст настоящей задачи 3" in sections[3]
        assert "текст после" not in sections[3]

    def test_duplicate_task_number_raises(self) -> None:
        text = "### Задача 1: разбор\nтекст\n### Задача 1: снова\nтекст\n"

        with pytest.raises(WiringInputError):
            READER.task_sections(text)


class TestTaskSectionsFences:
    """Fenced-содержимое не входит в текст раздела и не даёт заголовков (§5.3).

    Фенс — токен `fence` парсера: его содержимое не `inline`, поэтому блок
    покрытия внутри раздела не «ссылается» сам на все свои места.
    """

    def test_fenced_content_excluded_from_section_text(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "прозаический текст\n"
            "```python\n"
            "код внутри фенса\n"
            "```\n"
            "проза после фенса\n"
        )

        sections = READER.task_sections(text)

        assert "прозаический текст" in sections[1]
        assert "проза после фенса" in sections[1]
        assert "код внутри фенса" not in sections[1]

    def test_cover_block_content_excluded_from_section_text(self) -> None:
        text = (
            "### Task 1: unrelated work\n"
            "```disputatio-wiring-cover\n"
            'site = "src/pkg/b.py:8"\n'
            "```\n"
        )

        sections = READER.task_sections(text)

        assert "src/pkg/b.py:8" not in sections[1]

    def test_task_heading_inside_fence_is_not_heading(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "```markdown\n"
            "### Task 2: пример заголовка в коде\n"
            "```\n"
            "проза задачи 1 после фенса\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "проза задачи 1 после фенса" in sections[1]

    def test_shorter_fence_line_does_not_close_longer_fence(self) -> None:
        """```` открыт — строка ``` внутри его не закрывает (CommonMark)."""
        text = (
            "### Задача 1: разбор\n"
            "````markdown\n"
            "```\n"
            "### Task 2: всё ещё внутри фенса\n"
            "внутри фенса\n"
            "```\n"
            "````\n"
            "проза после\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "внутри фенса" not in sections[1]
        assert "проза после" in sections[1]

    def test_fence_line_with_info_does_not_close_fence(self) -> None:
        """Закрывающая строка — только из кавычек: ```` ```x ```` не закрывает."""
        text = (
            "### Задача 1: разбор\n"
            "```\n"
            "```python\n"
            "### Task 2: всё ещё внутри фенса\n"
            "```\n"
            "проза после\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "проза после" in sections[1]

    def test_tilde_fence_hides_heading_and_content(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "~~~\n"
            "### Task 2: внутри тильдового фенса\n"
            "```\n"
            "тильдовое содержимое\n"
            "~~~\n"
            "проза после\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "тильдовое содержимое" not in sections[1]
        assert "проза после" in sections[1]

    def test_inline_code_at_line_start_is_not_fence(self) -> None:
        """```` ```x``` ```` в начале строки — инлайн-код, фенс не открывается."""
        text = (
            "### Задача 1: разбор\n"
            "```inline``` проза задачи 1\n"
            "### Task 2: настоящий заголовок\n"
            "проза задачи 2\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1, 2}
        assert "проза задачи 1" in sections[1]


class TestFenceIndentation:
    """Строка фенса — не более 3 пробелов отступа (CommonMark), как §5.3.

    Без этого ограничения строка примера с отступом 4+ (частый случай —
    иллюстрация фенса внутри фенса) закрывала бы или открывала бы блок
    раньше времени, и код/заголовок внутри примера утекал бы в прозу.
    """

    def test_four_space_indent_does_not_close_fence(self) -> None:
        text = (
            "### Задача 1: разбор\n```text\n    ```\nвнутри фенса\n```\nпроза после\n"
        )

        sections = READER.task_sections(text)

        assert "внутри фенса" not in sections[1]
        assert "проза после" in sections[1]

    def test_four_space_indent_does_not_open_fence(self) -> None:
        text = "### Задача 1: разбор\n    ```\nпроза с отступом 4 остаётся прозой\n"

        sections = READER.task_sections(text)

        assert "проза с отступом 4 остаётся прозой" in sections[1]

    def test_tab_indent_is_not_a_fence_line(self) -> None:
        text = "### Задача 1: разбор\n\t```\nпроза после табуляции\n"

        sections = READER.task_sections(text)

        assert "проза после табуляции" in sections[1]

    @pytest.mark.parametrize("indent", ["", " ", "  ", "   "], ids=["0", "1", "2", "3"])
    def test_indent_zero_to_three_still_opens_and_closes(self, indent: str) -> None:
        text = (
            "### Задача 1: разбор\n"
            f"{indent}```python\n"
            "код внутри фенса\n"
            f"{indent}```\n"
            "проза после\n"
        )

        sections = READER.task_sections(text)

        assert "код внутри фенса" not in sections[1]
        assert "проза после" in sections[1]


class TestCommonMarkHeadingBoundaries:
    """Граница раздела — любой заголовок 1–3 уровня верхнего уровня (§5.3).

    Заголовок, который рендерится как заголовок, но не обрывает раздел,
    засчитал бы место под ним предыдущей задаче — тихий пропуск (код 0).
    """

    @pytest.mark.parametrize(
        "heading",
        [
            pytest.param("##  Примечания", id="two-spaces"),
            pytest.param("   ## Примечания", id="indent-3"),
            pytest.param(" # Примечания", id="indent-1-level-1"),
            pytest.param("##\tПримечания", id="tab"),
            pytest.param("##", id="bare-hashes"),
            pytest.param("## ", id="hashes-and-space"),
            pytest.param("Примечания\n===", id="setext-level-1"),
            pytest.param("Примечания\n---", id="setext-level-2"),
            pytest.param("Примечания\n  ----  ", id="setext-indented-trailing"),
        ],
    )
    def test_heading_form_ends_task_section(self, heading: str) -> None:
        text = (
            f"### Задача 1: разбор\nтело задачи 1\n\n{heading}\nхвост после заголовка\n"
        )

        sections = READER.task_sections(text)

        assert "тело задачи 1" in sections[1]
        assert "хвост после заголовка" not in sections[1]

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("####  Подраздел", id="level-4"),
            pytest.param("#5 без пробела", id="no-space"),
            pytest.param("    ## отступ 4 — код", id="indent-4"),
        ],
    )
    def test_non_heading_does_not_end_section(self, line: str) -> None:
        text = f"### Задача 1: разбор\nтело задачи 1\n{line}\nвсё ещё тело\n"

        sections = READER.task_sections(text)

        assert "всё ещё тело" in sections[1]

    def test_thematic_break_after_blank_line_is_not_boundary(self) -> None:
        text = "### Задача 1: разбор\nтело задачи 1\n\n---\nвсё ещё тело задачи 1\n"

        sections = READER.task_sections(text)

        assert "всё ещё тело задачи 1" in sections[1]

    def test_setext_underline_inside_fence_is_not_boundary(self) -> None:
        text = "### Задача 1: разбор\n```\nПример\n===\n```\nпроза задачи 1\n"

        sections = READER.task_sections(text)

        assert "проза задачи 1" in sections[1]

    def test_setext_task_like_heading_is_boundary_not_task(self) -> None:
        text = "### Задача 1: разбор\nтело задачи 1\nTask 2: текст\n---\nхвост\n"

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "хвост" not in sections[1]

    @pytest.mark.parametrize(
        "heading",
        [
            pytest.param("###  Задача 3:  разбор", id="two-spaces"),
            pytest.param("  ### Задача 3: разбор", id="indent-2"),
            pytest.param("###\tTask\t3\t: разбор", id="tabs"),
        ],
    )
    def test_task_heading_whitespace_variants_still_task(self, heading: str) -> None:
        text = f"{heading}\nтело задачи 3\n"

        sections = READER.task_sections(text)

        assert set(sections) == {3}
        assert "тело задачи 3" in sections[3]


class TestFenceScannerForBlocks:
    def test_block_closed_only_by_fence_of_same_length_or_longer(self) -> None:
        """Блок в ````-фенсе: строка ``` внутри — литерал, а не конец блока."""
        body = 'src_tree = """\n```\n"""\n'
        text = f"````disputatio-wiring-cover\n{body}````\n"

        block = parse_cover(text, reader=READER)

        assert block.src_tree == "```\n"

    def test_block_inside_tilde_fence_not_read(self) -> None:
        """Блок внутри `~~~`-фенса — литеральный текст примера, не блок."""
        text = "~~~\n```disputatio-wiring\n" + EXAMPLE_RULES_BODY + "```\n~~~\n"

        with pytest.raises(WiringInputError):
            parse_rules(text, reader=READER)


class TestTaskSectionsHtmlComments:
    """HTML-комментарий вне фенса не рендерится — не заголовок и не текст (§5.3).

    Комментарий парсер отдаёт токеном `html_block`/`html_inline`, а они
    текста не дают; незакрытый `<!--` тянется до конца документа
    (CommonMark, HTML-блок типа 2). Иначе невидимый `### Task N:` создавал бы
    задачу, а невидимое место засчитывалось бы ссылкой — тихий код 0.
    """

    def test_task_heading_inside_block_comment_is_not_task(self) -> None:
        text = "<!--\n### Task 1: скрытая\nsrc/pkg/guard.py:1 b\n-->\n"

        assert READER.task_sections(text) == {}

    def test_unclosed_comment_hides_rest_of_document(self) -> None:
        text = "### Задача 1: видимая\nтело\n<!--\n### Task 2: скрытая\nsrc/x.py:1\n"

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "src/x.py:1" not in sections[1]

    def test_site_inside_comment_is_not_section_text(self) -> None:
        text = (
            "### Задача 1: разбор\n"
            "видимый текст <!-- src/pkg/a.py:3 --> и хвост\n"
            "<!--\nsrc/pkg/b.py:4\n-->\n"
            "после комментария\n"
        )

        section = READER.task_sections(text)[1]

        assert "src/pkg/a.py:3" not in section
        assert "src/pkg/b.py:4" not in section
        assert "видимый текст" in section
        assert "и хвост" in section
        assert "после комментария" in section

    def test_hidden_heading_is_not_a_boundary(self) -> None:
        """Невидимый заголовок раздел не обрывает: текст под ним — задачи 1."""
        text = "### Задача 1: разбор\n<!--\n## Скрыто\n-->\nтекст задачи 1\n"

        assert "текст задачи 1" in READER.task_sections(text)[1]

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("<!-- x -->### Task 2: мнимая", id="after-inline"),
            pytest.param("<!-- x --> ### Task 2: мнимая", id="after-inline-space"),
            pytest.param("   <!-- x -->### Task 2: мнимая", id="indented"),
            pytest.param("<!---->### Task 2: мнимая", id="empty-comment"),
            pytest.param("<!-->### Task 2: мнимая", id="short-comment"),
        ],
    )
    def test_line_starting_with_comment_is_not_heading(self, line: str) -> None:
        """Строка, начатая `<!--`, — HTML-блок до конца строки, не заголовок."""
        text = f"### Задача 1: разбор\n{line}\n"

        assert set(READER.task_sections(text)) == {1}

    @pytest.mark.parametrize("comment", ["<!-->", "<!--->", "<!---->"])
    def test_short_comment_closes_immediately(self, comment: str) -> None:
        """`<!-->` и `<!--->` — законченные комментарии (CommonMark)."""
        text = f"### Задача 1: разбор\n{comment}\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in READER.task_sections(text)[1]

    def test_line_ending_comment_is_not_heading(self) -> None:
        text = "### Задача 1: разбор\n<!--\nскрыто\n--> ### Task 2: мнимая\n"

        assert set(READER.task_sections(text)) == {1}

    def test_heading_with_trailing_inline_comment_is_task(self) -> None:
        text = "### Task 3: разбор <!-- заметка -->\nтело задачи 3\n"

        sections = READER.task_sections(text)

        assert set(sections) == {3}
        assert "заметка" not in sections[3]

    def test_comment_marker_inside_fence_is_literal(self) -> None:
        text = "### Задача 1: разбор\n```html\n<!--\n```\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in READER.task_sections(text)[1]

    def test_fence_inside_comment_is_not_block(self) -> None:
        """Единственный блок внутри комментария — не блок: код 2."""
        text = "<!--\n```disputatio-wiring\n" + EXAMPLE_RULES_BODY + "```\n-->\n"

        with pytest.raises(WiringInputError, match="не найден"):
            parse_rules(text, reader=READER)

    def test_fence_inside_comment_does_not_hide_following_text(self) -> None:
        """Открытая в комментарии «ограда» фенсом не считается и прозу не ест."""
        text = "### Задача 1: разбор\n<!--\n```\n-->\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in READER.task_sections(text)[1]


class TestTaskSectionsRawHtml:
    """Сырые HTML-блоки плана: гейт не видит в них больше, чем рендер (§5.3).

    `<script>`/`<style>`/`<pre>`/`<textarea>` (HTML-блок типа 1) —
    сырой текст до закрывающего тега: не заголовки и не текст раздела.
    Прочие HTML-блоки (типы 6–7) тянутся до пустой строки: markdown в них не
    разбирается, поэтому строка `### Task N:` в них не заголовок, а их текст
    — не текст раздела.
    """

    @pytest.mark.parametrize("tag", ["script", "style", "pre", "textarea", "PRE"])
    def test_type1_block_hides_heading_and_text(self, tag: str) -> None:
        text = (
            "### Задача 1: разбор\nвидимо\n"
            f"<{tag}>\n### Task 2: скрытая\nsrc/pkg/a.py:3\n</{tag}> хвост\n"
            "после блока\n"
        )

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "src/pkg/a.py:3" not in sections[1]
        assert "хвост" not in sections[1]
        assert "после блока" in sections[1]

    def test_unclosed_type1_block_hides_rest(self) -> None:
        text = "<script>\n### Task 1: скрытая\nsrc/pkg/a.py:3\n"

        assert READER.task_sections(text) == {}

    def test_type1_block_closes_on_same_line(self) -> None:
        text = "### Задача 1: разбор\n<pre>x</pre>\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in READER.task_sections(text)[1]

    @pytest.mark.parametrize("tag", ["div", "details", "span", "/div", "table"])
    def test_task_heading_inside_html_block_is_not_task(self, tag: str) -> None:
        text = f"<{tag}>\n### Task 1: не заголовок\nsrc/pkg/a.py:3\n"

        assert READER.task_sections(text) == {}

    def test_html_block_content_is_not_section_text(self) -> None:
        text = "### Задача 1: разбор\n\n<div>\n### Task 2: x\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" not in READER.task_sections(text)[1]

    def test_html_block_ends_at_blank_line(self) -> None:
        text = "<details>\n<summary>x</summary>\n\n### Task 1: видимая\nтело\n"

        assert set(READER.task_sections(text)) == {1}

    def test_comment_line_does_not_end_html_block(self) -> None:
        """Строка-комментарий внутри HTML-блока — не пустая строка блока."""
        text = "<div>\n<!-- c -->\n### Task 1: не заголовок\n"

        assert READER.task_sections(text) == {}


class TestTaskSectionsLinkReferenceDefinitions:
    """Определение ссылки `[x]: …` не рендерится — не текст раздела (§5.3)."""

    @pytest.mark.parametrize(
        "definition",
        [
            pytest.param("[n]: src/pkg/a.py:3", id="plain"),
            pytest.param("   [n]: src/pkg/a.py:3", id="indent-3"),
            pytest.param("[^1]: src/pkg/a.py:3", id="footnote"),
            pytest.param("[n]:\n  src/pkg/a.py:3", id="destination-next-line"),
            pytest.param('[n]: /u\n  "src/pkg/a.py:3"', id="title-next-line"),
        ],
    )
    def test_definition_text_excluded(self, definition: str) -> None:
        text = f"### Задача 1: разбор\nвидимо\n\n{definition}\n\nпосле\n"

        section = READER.task_sections(text)[1]

        assert "src/pkg/a.py:3" not in section
        assert "видимо" in section
        assert "после" in section

    def test_heading_after_definition_still_heading(self) -> None:
        text = "### Задача 1: разбор\n[n]: /u\n### Task 2: вторая\nтело 2\n"

        sections = READER.task_sections(text)

        assert set(sections) == {1, 2}
        assert "тело 2" in sections[2]


class TestTaskSectionsContainerHeadings:
    """Заголовок в цитате или пункте списка — не задача и не граница (§5.3).

    Раздел обрывает только заголовок `h1`–`h3` верхнего уровня документа.
    Прежний ручной разбор считал такой заголовок границей (сужение раздела);
    по решению владельца граница и задача — только на верхнем уровне.
    """

    @pytest.mark.parametrize(
        "heading",
        [
            pytest.param("> ### Task 2: в цитате", id="quote"),
            pytest.param(">## Примечания", id="quote-no-space"),
            pytest.param("> > # Глубже", id="nested-quote"),
            pytest.param("- ### Task 2: пункт", id="bullet"),
            pytest.param("* ## Пункт", id="star"),
            pytest.param("1. # Пункт", id="ordered"),
            pytest.param("- пункт\n  - ### Task 2: вложенный", id="nested-list"),
            pytest.param("> Примечания\n> ---", id="setext-in-quote"),
        ],
    )
    def test_container_heading_does_not_end_section(self, heading: str) -> None:
        text = f"### Задача 1: разбор\nтело\n\n{heading}\n\nsrc/pkg/a.py:3\n"

        sections = READER.task_sections(text)

        assert set(sections) == {1}
        assert "тело" in sections[1]
        assert "src/pkg/a.py:3" in sections[1]

    @pytest.mark.parametrize("line", ["- #123 задача", "- пункт ### не в начале"])
    def test_list_lazy_continuation_is_section_text(self, line: str) -> None:
        text = f"### Задача 1: разбор\n{line}\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" in READER.task_sections(text)[1]

    def test_quote_lazy_continuation_is_not_section_text(self) -> None:
        """Ленивое продолжение абзаца цитаты — текст цитаты, а он не считается."""
        text = "### Задача 1: разбор\n> #5 без пробела\nsrc/pkg/a.py:3\n"

        assert "src/pkg/a.py:3" not in READER.task_sections(text)[1]


class TestTaskHeadingNumber:
    def test_non_ascii_digits_are_not_task_number(self) -> None:
        """`\\d` Python матчит `١`; номер задачи — только ASCII-цифры."""
        text = "### Task ١: разбор\nsrc/pkg/a.py:3\n"

        assert READER.task_sections(text) == {}

    def test_escaped_heading_is_not_task(self) -> None:
        assert READER.task_sections("\\### Task 1: разбор\nтело\n") == {}

    def test_closing_hashes_keep_task_heading(self) -> None:
        assert set(READER.task_sections("### Task 1: разбор ###\nтело\n")) == {1}


class TestVisibleText:
    """Видимый текст раздела — `inline` абзацев и заголовков вне цитат (§5.3)."""

    def test_task_heading_text_is_section_text(self) -> None:
        assert "src/use.py:2" in READER.task_sections("### Task 1: src/use.py:2\n")[1]

    def test_list_heading_is_not_task(self) -> None:
        text = "- пункт\n\n  ### Task 1: в пункте\n"

        assert READER.task_sections(text) == {}

    def test_quote_heading_is_not_task(self) -> None:
        assert READER.task_sections("> ### Task 1: в цитате\n") == {}

    def test_blocks_are_separated_by_newline(self) -> None:
        text = "### Task 1: t\n\n- src/use.py:\n- 2\n"

        assert "src/use.py:\n2" in READER.task_sections(text)[1]

    def test_softbreak_is_space(self) -> None:
        text = "### Task 1: t\n\nsrc/use.py:\n2\n"

        assert "src/use.py: 2" in READER.task_sections(text)[1]

    def test_link_label_counts_href_and_title_do_not(self) -> None:
        text = '### Task 1: t\n\n[метка](https://x/адрес "заголовок")\n'

        section = READER.task_sections(text)[1]

        assert "метка" in section
        assert "адрес" not in section
        assert "заголовок" not in section

    def test_image_alt_is_not_text(self) -> None:
        text = "### Task 1: t\n\n![src/use.py:2](i.png)\n"

        assert "src/use.py:2" not in READER.task_sections(text)[1]

    def test_emphasis_concatenates_as_rendered(self) -> None:
        text = "### Task 1: t\n\nsrc/use.py:**2**\n"

        assert "src/use.py:2" in READER.task_sections(text)[1]

    def test_task_heading_with_inline_markup(self) -> None:
        assert set(READER.task_sections("### **Task 1:** t\n")) == {1}


class TestFencedBlocks:
    """`fenced_blocks` — только настоящие фенсы с info-строкой ровно `info`."""

    def test_fence_in_list_item_is_block(self) -> None:
        text = "- пункт\n\n  ```x\n  a = 1\n  ```\n"

        assert READER.fenced_blocks(text, "x") == ["a = 1\n"]

    def test_fence_inside_html_block_is_not_block(self) -> None:
        text = "<div>\n```x\na = 1\n```\n</div>\n"

        assert READER.fenced_blocks(text, "x") == []

    def test_info_must_match_exactly(self) -> None:
        text = "```x y\na\n```\n"

        assert READER.fenced_blocks(text, "x") == []

    def test_indented_code_is_not_block(self) -> None:
        assert READER.fenced_blocks("    ```x\n    a\n    ```\n", "x") == []
