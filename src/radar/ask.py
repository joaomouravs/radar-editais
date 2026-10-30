"""Free-form questions about one edital, answered from cited passages.

The extraction schema has fixed fields with known keywords. A question can be about
anything ("is a technical capacity certificate required?"), so passages are ranked with
BM25 over the question's words instead, and every citation in the answer is checked
against the page it names before it is shown.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from radar.annotate import fold
from radar.evaluate import ground
from radar.llm import ChatClient, LlmError, Model
from radar.retrieve import Span, merge, render
from radar.schema import strict_json_schema

CHUNK_CHARS = 1_000
BUDGET_CHARS = 10_000

# Words too common in editais, or in questions, to say anything about relevance.
_STOPWORDS = """
a ao aos as com como da das de do dos e em entre esta este foi ha isso mais mas na nas no
nos o os ou para pela pelas pelo pelos por qual quais quando que se sem ser sera sobre sua
suas tem um uma uns umas edital licitacao licitante pregao item
"""
STOPWORDS = frozenset(_STOPWORDS.split())

SYSTEM_PROMPT = """\
You answer questions about one Brazilian public procurement notice (edital de \
licitação). You are given excerpts of it, each tagged with its page as <page n>; [...] \
marks text left out.

Rules:
- Answer only from the excerpts, in Portuguese, in at most three sentences.
- Cite every passage you rely on with its page and a short verbatim quote, copied \
exactly.
- If the excerpts do not answer the question, set answerable to false, say what is \
missing, and cite nothing. Do not answer from general knowledge of procurement law."""


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    quote: str = Field(description="Verbatim passage from that page, at most 300 characters.")


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answerable: bool = Field(description="Whether the excerpts answer the question.")
    answer: str
    citations: list[Citation]


@dataclass
class Checked:
    citation: Citation
    found: str  # on_page, other_page or not_found, as in the evaluation


@dataclass
class Reply:
    answer: Answer | None
    checks: list[Checked]
    pages_sent: list[int]
    error: str | None = None


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]{3,}", fold(text)) if w not in STOPWORDS]


def chunks(pages: list[str], size: int = CHUNK_CHARS) -> list[Span]:
    """Each page cut into pieces of about `size` characters, at whitespace."""
    spans = []
    for number, text in enumerate(pages, 1):
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            if end < len(text):
                space = text.rfind(" ", start + size // 2, end)
                end = space if space > start else end
            spans.append(Span(number, start, end))
            start = end
    return spans


def rank(pages: list[str], question: str, *, k1: float = 1.5, b: float = 0.75) -> list[Span]:
    """Chunks ordered by BM25 score for the question's words, best first."""
    candidates = chunks(pages)
    documents = [Counter(words(pages[s.page - 1][s.start : s.end])) for s in candidates]
    if not documents:
        return []
    average = sum(sum(d.values()) for d in documents) / len(documents) or 1
    frequency = Counter(term for d in documents for term in d)
    terms = set(words(question))

    def score(document: Counter[str]) -> float:
        length = sum(document.values())
        total = 0.0
        for term in terms & document.keys():
            idf = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            tf = document[term]
            total += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / average))
        return total

    scored = sorted(zip(candidates, map(score, documents), strict=True), key=lambda p: -p[1])
    return [span for span, value in scored if value > 0]


def select(pages: list[str], question: str, budget: int = BUDGET_CHARS) -> list[Span]:
    chosen: list[Span] = []
    for span in rank(pages, question):
        candidate = merge([*chosen, span])
        if sum(s.end - s.start for s in candidate) > budget:
            break
        chosen = candidate
    return chosen


def ask(client: ChatClient, model: Model, pages: list[str], question: str) -> Reply:
    spans = select(pages, question)
    if not spans:
        return Reply(None, [], [], "no passage shares a word with the question")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{render(pages, spans)}\n\nQuestion: {question}"},
    ]
    sent = sorted({s.page for s in spans})
    try:
        completion = client.complete(model, messages, strict_json_schema(Answer))
        answer = Answer.model_validate_json(completion.content)
    except (LlmError, ValidationError) as exc:
        return Reply(None, [], sent, str(exc))
    checks = [Checked(c, ground(c.quote, c.page, pages)) for c in answer.citations]
    return Reply(answer, checks, sent)
