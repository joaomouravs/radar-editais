"""The retrieve strategy: send excerpts around each field's keywords instead of the whole
edital.

Groq's free tier allows 8,000 tokens per minute and the median edital in the evaluation
set is about 29,000 tokens, so the whole document does not fit in one request. The
editais mostly follow the federal AGU template, whose section titles and stock phrases
are predictable, which is why keywords come before embeddings: embeddings are only worth
adding if the evaluation shows the keywords missing the passages that hold the answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from radar.annotate import fold

# Keywords are matched lowercase and without accents.
FIELD_KEYWORDS = {
    "estimated_value": ["valor estimado", "valor total", "valor global", "valor maximo"],
    "proposal_deadline": ["sessao publica", "data de abertura", "envio de propostas"],
    "award_criterion": ["criterio de julgamento"],
    "dispute_mode": ["modo de disputa"],
    "price_registration": ["registro de precos"],
    "small_business": ["exclusiva", "exclusivo", "cota reservada", "cota de ate"],
    "site_visit": ["vistoria", "visita tecnica"],
    "performance_guarantee_pct": ["garantia de execucao", "garantia contratual", "garantia da"],
    "contract_term_months": ["vigencia"],
    "consortium_allowed": ["consorcio"],
}

# Measured on Groq: 15,000 characters of excerpts made a 5,600-token prompt (the
# instructions and the schema alone are about 2,300), and a retry, which adds the
# rejected answer and the error, then asked for 8,163 tokens. 12,000 keeps the retry
# under the 8,000-token limit.
BUDGET_CHARS = 12_000
WINDOW_CHARS = 350
# The opening of the edital (its summary box, in the AGU template) states the value,
# the session date, the criterion and the dispute mode together.
PREAMBLE_CHARS = 1_800
MATCHES_PER_FIELD = 4


@dataclass
class Span:
    page: int  # 1-based
    start: int
    end: int


def select(pages: list[str], budget: int = BUDGET_CHARS) -> list[Span]:
    """Spans to send: the preamble, then a window around keyword matches, taking one
    match per field in turn so every field gets context before any field gets more."""
    spans = [Span(1, 0, min(PREAMBLE_CHARS, len(pages[0])))] if pages else []
    folded = [fold(text) for text in pages]
    queues = []
    for keywords in FIELD_KEYWORDS.values():
        pattern = re.compile("|".join(re.escape(k) for k in keywords))
        matches = [
            Span(n, max(0, m.start() - WINDOW_CHARS), min(len(text), m.end() + WINDOW_CHARS))
            for n, text in enumerate(folded, 1)
            for m in pattern.finditer(text)
        ]
        queues.append(matches[:MATCHES_PER_FIELD])

    while any(queues):
        for queue in queues:
            if not queue:
                continue
            candidate = queue.pop(0)
            merged = merge([*spans, candidate])
            if sum(s.end - s.start for s in merged) > budget:
                return spans
            spans = merged
    return spans


def merge(spans: list[Span]) -> list[Span]:
    """Sorted spans with overlapping or touching ones on the same page joined."""
    merged: list[Span] = []
    for span in sorted(spans, key=lambda s: (s.page, s.start)):
        last = merged[-1] if merged else None
        if last and last.page == span.page and span.start <= last.end:
            last.end = max(last.end, span.end)
        else:
            merged.append(Span(span.page, span.start, span.end))
    return merged


def render(pages: list[str], spans: list[Span]) -> str:
    """Excerpts tagged with their page, so evidence still cites real pages."""
    by_page: dict[int, list[str]] = {}
    for span in spans:
        by_page.setdefault(span.page, []).append(pages[span.page - 1][span.start : span.end])
    return "\n".join(
        f"<page {n}>\n" + "\n[...]\n".join(part.strip() for part in parts) + f"\n</page {n}>"
        for n, parts in sorted(by_page.items())
    )
