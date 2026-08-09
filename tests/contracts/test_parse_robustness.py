"""Тесты TASK-020: робастность parse_proposal (CRLF, ведущая пустая строка).

[REQ-020], [REQ-003], [DESIGN-018]: агентский вывод не byte-stable (ADR-008),
поэтому вход нормализуется до разбора — CRLF-версия документа даёт тот же
фронтматтер и LF-тело, что LF-версия; ведущие пустые строки отбрасываются.
Нормализация не ослабляет структурные проверки REQ-003: ведущий пробел перед
`---` и незакрытый фронтматтер падают по-прежнему. Новый файл — тест-файлы
предыдущих итераций байт-неизменны (NFR-003). Red-селектор
(`test_crlf_spec_example_matches_lf`) превращает ProposalParseError в
AssertionError — гейт принимает red только при падении assertion'ом.
"""

import pytest

from disputatio.contracts.proposal import ProposalParseError, parse_proposal

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

LF_TEXT = f"---\n{SPEC_4_2_FRONTMATTER}---\n{SPEC_4_2_BODY}"
CRLF_TEXT = LF_TEXT.replace("\n", "\r\n")


def test_crlf_spec_example_matches_lf() -> None:
    """CRLF-версия примера §4.2 даёт тот же фронтматтер и LF-тело."""
    lf_frontmatter, lf_body = parse_proposal(LF_TEXT)
    try:
        crlf_frontmatter, crlf_body = parse_proposal(CRLF_TEXT)
    except ProposalParseError as exc:  # red-фаза: CRLF ещё не нормализуется
        raise AssertionError(f"CRLF-вход отвергнут: {exc}") from exc
    assert crlf_frontmatter == lf_frontmatter
    assert crlf_body == lf_body == SPEC_4_2_BODY


def test_leading_blank_line_allowed() -> None:
    """Ведущая пустая строка перед `---` допускается (LF- и CRLF-вход)."""
    expected_frontmatter, expected_body = parse_proposal(LF_TEXT)
    for text in (f"\n{LF_TEXT}", "\r\n" + CRLF_TEXT):
        frontmatter, body = parse_proposal(text)
        assert frontmatter == expected_frontmatter
        assert body == expected_body


def test_mixed_crlf_and_leading_blank_line() -> None:
    """Смешанный вход — перевод строки одного вида, документ другого."""
    expected_frontmatter, expected_body = parse_proposal(LF_TEXT)
    for text in ("\n" + CRLF_TEXT, "\r\n" + LF_TEXT):
        frontmatter, body = parse_proposal(text)
        assert frontmatter == expected_frontmatter
        assert body == expected_body


def test_leading_space_still_rejected() -> None:
    """`" ---"` — ведущий пробел не пустая строка: ProposalParseError."""
    with pytest.raises(ProposalParseError):
        parse_proposal(" " + LF_TEXT)


def test_crlf_unclosed_frontmatter_still_fails() -> None:
    """CRLF-вход без закрывающей строки `---` → ProposalParseError."""
    unclosed = f"---\n{SPEC_4_2_FRONTMATTER}".replace("\n", "\r\n")
    with pytest.raises(ProposalParseError):
        parse_proposal(unclosed)
