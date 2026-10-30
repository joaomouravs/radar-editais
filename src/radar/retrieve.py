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

# Bumped whenever the selection changes, so runs over different excerpts stay apart.
VERSION = "2"

# Per field, patterns in priority order, matched on lowercase text without accents.
# Version 1 took the first four matches of one generic keyword per field, and the page
# that settles the answer was sent for only 16 of 31 contract terms and 2 of 5
# guarantees: "vigência" mostly appears in boilerplate ("durante a vigência do
# contrato") long before the clause that states the term. So the strong patterns look
# for the shape of the deciding clause (the keyword near a number, a percentage or a
# yes/no word) and the generic keyword only fills what is left.
_NUMBER = (
    r"(\d+(,\d+)?\s*\(|\(\w+\)\s*(meses|mes|ano|anos|dias)|\d+\s*(meses|ano|anos|dias)"
    r"|\b(um|dois|tres|doze) (ano|anos|meses)\b)"
)
FIELD_PATTERNS: dict[str, list[str]] = {
    "estimated_value": [
        r"valor (total |global |maximo )?(estimad\w|global|maximo|total).{0,80}r\$",
        r"valor (estimado|total|global|maximo)",
    ],
    "proposal_deadline": [
        r"(sessao publica|abertura|envio de propostas|recebimento das propostas).{0,120}"
        r"\d{1,2}/\d{1,2}/20\d\d",
        r"sessao publica|data de abertura|envio de propostas",
    ],
    "award_criterion": [r"criterio de julgamento"],
    "dispute_mode": [r"modo de disputa"],
    "price_registration": [r"(sistema de|para) registro de precos", r"registro de precos"],
    "small_business": [
        r"(exclusiv\w*|cota reservada|cota de ate).{0,80}(microempresa|me/epp|mei)",
        r"(microempresa|me/epp).{0,80}exclusiv",
        r"exclusiva|exclusivo|cota reservada|cota de ate",
    ],
    "site_visit": [
        r"(vistoria|visita tecnica).{0,200}"
        r"(facultativ|faculdade|obrigatori|opt[ae]|nao se aplica|substitu|sob pena)",
        r"(facultativ|obrigatori).{0,80}(vistoria|visita tecnica)",
        r"vistoria|visita tecnica",
    ],
    "performance_guarantee_pct": [
        r"garantia (de execucao|contratual|da contratacao|do contrato).{0,200}"
        r"(\d+(,\d+)?\s*%|nao havera|nao sera|dispensad)",
        r"garantia.{0,120}\d+(,\d+)?\s*%",
        r"(nao havera|nao sera) exig\w+ (da |de )?garantia",
        r"\d+(,\d+)?\s*%.{0,80}garantia",
        r"garantia de execucao|garantia contratual",
    ],
    "contract_term_months": [
        r"(prazo de vigencia|vigencia (da|do|de)|validade da ata|vigorara).{0,120}" + _NUMBER,
        r"ata de registro de precos (tera|sera|vigorara).{0,60}(vigencia|validade)",
        r"prazo de vigencia|vigencia da ata|validade da ata",
    ],
    "consortium_allowed": [
        r"(vedad\w|nao sera (admitid|permitid)\w*|sera (admitid|permitid)\w*).{0,80}consorcio",
        r"reunidas em consorcio|participacao (de empresas )?em consorcio",
        r"consorcio",
    ],
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
    for patterns in FIELD_PATTERNS.values():
        matches: list[Span] = []
        seen: set[tuple[int, int]] = set()
        # Every match of the first pattern, then of the second..., each in page order.
        for pattern in map(lambda p: re.compile(p, re.S), patterns):
            for n, text in enumerate(folded, 1):
                for m in pattern.finditer(text):
                    if (n, m.start()) in seen:
                        continue
                    seen.add((n, m.start()))
                    start = max(0, m.start() - WINDOW_CHARS)
                    matches.append(Span(n, start, min(len(text), m.end() + WINDOW_CHARS)))
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
