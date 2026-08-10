"""Тесты parse_proposal и ProposalFrontmatter: TASK-004, [DESIGN-003], [REQ-003].

Импорты `disputatio.contracts.proposal` выполняются внутри тестов: на момент
red-чекпоинта модуля ещё нет, и импорт на уровне модуля сломал бы collection.
Red-селектор (`test_spec_4_2_example_parses`) превращает ImportError в
AssertionError — гейт принимает red только при падении assertion'ом.
"""

import pytest
from pydantic import ValidationError

from disputatio.contracts.base import Role

SPEC_4_2_FRONTMATTER = (
    "schema: disputatio/v1\n"
    "round: 3\n"
    "role: author\n"
    "responds_to: rounds/002/review.json\n"
    'files_touched: ["src/x.py", "docs/y.md"]\n'
    "self_declared_status: complete\n"
)

SPEC_4_2_BODY = (
    "# Раунд 3\n"
    "\n"
    "Что сделано, почему так, спорные места.\n"
    "\n"
    "---\n"
    "\n"
    "Тематический разрыв выше — не граница фронтматтера.\n"
)


def make_proposal(frontmatter: str, body: str) -> str:
    """Собирает текст proposal.md из YAML-блока и тела markdown."""
    return f"---\n{frontmatter}---\n{body}"


def test_spec_4_2_example_parses() -> None:
    """Пример §4.2 разбирается: валидированная модель + тело без изменений."""
    try:
        from disputatio.contracts.proposal import parse_proposal
    except ImportError as exc:  # red-фаза: proposal.py ещё не создан
        raise AssertionError(
            "src/disputatio/contracts/proposal.py ещё не создан"
        ) from exc

    text = make_proposal(SPEC_4_2_FRONTMATTER, SPEC_4_2_BODY)
    frontmatter, body = parse_proposal(text)
    assert frontmatter.round == 3
    assert frontmatter.role == Role.AUTHOR
    assert frontmatter.responds_to == "rounds/002/review.json"
    assert frontmatter.files_touched == ["src/x.py", "docs/y.md"]
    assert frontmatter.self_declared_status == "complete"
    assert body == SPEC_4_2_BODY


def test_body_returned_unchanged() -> None:
    """Тело возвращается байт-в-байт: пустое, без финального \\n, с `---`."""
    from disputatio.contracts.proposal import parse_proposal

    for body in ("", "без завершающего перевода строки", "---\nещё --- внутри\n"):
        _, parsed_body = parse_proposal(make_proposal(SPEC_4_2_FRONTMATTER, body))
        assert parsed_body == body


def test_responds_to_none_allowed_in_round_one() -> None:
    """`responds_to: null` допустим — в раунде 1 отвечать не на что."""
    from disputatio.contracts.proposal import parse_proposal

    block = SPEC_4_2_FRONTMATTER.replace("round: 3", "round: 1").replace(
        "responds_to: rounds/002/review.json", "responds_to: null"
    )
    frontmatter, _ = parse_proposal(make_proposal(block, SPEC_4_2_BODY))
    assert frontmatter.round == 1
    assert frontmatter.responds_to is None


def test_partial_status_accepted() -> None:
    """Второе допустимое значение статуса — `partial`."""
    from disputatio.contracts.proposal import parse_proposal

    block = SPEC_4_2_FRONTMATTER.replace(
        "self_declared_status: complete", "self_declared_status: partial"
    )
    frontmatter, _ = parse_proposal(make_proposal(block, SPEC_4_2_BODY))
    assert frontmatter.self_declared_status == "partial"


def test_missing_frontmatter_raises_parse_error() -> None:
    """Документ без открывающего `---\\n` → ProposalParseError (ValueError)."""
    from disputatio.contracts.proposal import ProposalParseError, parse_proposal

    assert issubclass(ProposalParseError, ValueError)
    no_frontmatter = (
        "",
        "# Просто markdown без фронтматтера\n",
        "---без перевода строки",
        " ---\nround: 1\n---\n",
    )
    for text in no_frontmatter:
        with pytest.raises(ProposalParseError):
            parse_proposal(text)


def test_unclosed_frontmatter_raises_parse_error() -> None:
    """Фронтматтер без закрывающей строки `---` → ProposalParseError."""
    from disputatio.contracts.proposal import ProposalParseError, parse_proposal

    with pytest.raises(ProposalParseError):
        parse_proposal(f"---\n{SPEC_4_2_FRONTMATTER}")


def test_invalid_yaml_raises_parse_error() -> None:
    """Синтаксически невалидный YAML во фронтматтере → ProposalParseError."""
    from disputatio.contracts.proposal import ProposalParseError, parse_proposal

    with pytest.raises(ProposalParseError):
        parse_proposal(make_proposal("files_touched: [unclosed\n", SPEC_4_2_BODY))


def test_yaml_non_dict_raises_parse_error() -> None:
    """YAML-скаляр, список или пустой блок → ProposalParseError."""
    from disputatio.contracts.proposal import ProposalParseError, parse_proposal

    for block in ("просто строка\n", "- a\n- b\n", ""):
        with pytest.raises(ProposalParseError):
            parse_proposal(make_proposal(block, SPEC_4_2_BODY))


def test_invalid_self_declared_status_rejected() -> None:
    """`self_declared_status` вне {complete, partial} → ValidationError."""
    from disputatio.contracts.proposal import parse_proposal

    for invalid in ("done", "in_progress", ""):
        block = SPEC_4_2_FRONTMATTER.replace(
            "self_declared_status: complete",
            f"self_declared_status: {invalid!r}",
        )
        with pytest.raises(ValidationError):
            parse_proposal(make_proposal(block, SPEC_4_2_BODY))


def test_role_only_author() -> None:
    """`role` — Literal[author]: reviewer и произвольные роли отклоняются."""
    from disputatio.contracts.proposal import parse_proposal

    for invalid in ("reviewer", "arbiter", ""):
        block = SPEC_4_2_FRONTMATTER.replace("role: author", f"role: {invalid!r}")
        with pytest.raises(ValidationError):
            parse_proposal(make_proposal(block, SPEC_4_2_BODY))


def test_missing_and_extra_fields_rejected() -> None:
    """Отсутствующее обязательное поле и лишний ключ → ValidationError."""
    from disputatio.contracts.proposal import parse_proposal

    without_round = SPEC_4_2_FRONTMATTER.replace("round: 3\n", "")
    with pytest.raises(ValidationError):
        parse_proposal(make_proposal(without_round, SPEC_4_2_BODY))

    with_extra = SPEC_4_2_FRONTMATTER + "extra_key: 1\n"
    with pytest.raises(ValidationError):
        parse_proposal(make_proposal(with_extra, SPEC_4_2_BODY))
