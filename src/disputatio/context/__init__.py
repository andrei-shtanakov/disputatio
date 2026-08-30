"""Сборка промптов автора и ревьюера из артефактов раунда (§6).

Публичный API пакета — четыре функции: `build_author_prompt` (§6.1) и
`build_reviewer_prompt` (§6.2) для develop/analyze-раундов ([DESIGN-001]),
`build_doc_author_prompt` (§5.1 SPEC-002) и `build_doc_reviewer_prompt`
(§5.2 SPEC-002) для doc-раундов пайплайна. Оркестратор импортирует только
их и только отсюда: шаг цикла выбирает пару по режиму сессии, и импорт
подмодуля был бы обходом той же границы, что закрыта для develop-пары.

`tags`, `sections`, `schema_rules` — деталь реализации и наружу не выходят.
Это не косметика: раскладка секций и текст правил §4.4 меняются вместе со
спецификацией, и единственная гарантия, что такая правка не потребует
трогать оркестратор, — отсутствие у него способа на них сослаться.
`TASK_TITLE` и прочие заголовки остаются публичными атрибутами своих
модулей ради тестов, но частью API пакета не становятся.

Пакет чист: ни I/O, ни времени, ни случайности (NFR-001, NFR-002).
"""

from disputatio.context.author import build_author_prompt
from disputatio.context.doc_author import build_doc_author_prompt
from disputatio.context.doc_reviewer import build_doc_reviewer_prompt
from disputatio.context.reviewer import build_reviewer_prompt

__all__ = [
    "build_author_prompt",
    "build_doc_author_prompt",
    "build_doc_reviewer_prompt",
    "build_reviewer_prompt",
]
