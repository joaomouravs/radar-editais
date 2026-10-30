"""Building the gold set: a fixed sample of notices, and the passages an annotator needs
to read to answer the fields the API does not have.

The annotator never sees what a model answered. Showing it would anchor the label to the
prediction, and the evaluation would end up measuring agreement with itself.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

KEYWORDS = {
    "site_visit": ["vistoria", "visita tecnica"],
    "performance_guarantee_pct": [
        "garantia de execucao",
        "garantia contratual",
        "garantia da contratacao",
        "garantia do contrato",
    ],
    "contract_term_months": ["vigencia"],
    "consortium_allowed": ["consorcio"],
}

QUESTIONS = {
    "site_visit": "Site visit: [m]andatory, [o]ptional, or [n]ot mentioned",
    "performance_guarantee_pct": "Performance guarantee: percentage (e.g. 5), or [n]one",
    "contract_term_months": "Contract or ata term in months, or [n]ot stated",
    "consortium_allowed": "Consortium: [y]es allowed, [n]o forbidden, or [-] not mentioned",
}


@dataclass
class Hit:
    page: int
    snippet: str


def sample(
    conn: sqlite3.Connection, size: int, *, per_body: int = 3, seed: int = 2026
) -> list[str]:
    """A reproducible sample of parsed notices with at most `per_body` from one public
    body, because one body reuses one template and would otherwise dominate the set."""
    rows = conn.execute(
        "SELECT DISTINCT n.control_number, n.cnpj FROM notices n "
        "JOIN pages p USING (control_number) ORDER BY n.control_number"
    ).fetchall()
    rng = random.Random(seed)
    shuffled = list(rows)
    rng.shuffle(shuffled)
    taken: Counter[str] = Counter()
    chosen: list[str] = []
    for row in shuffled:
        if taken[row["cnpj"]] < per_body:
            taken[row["cnpj"]] += 1
            chosen.append(row["control_number"])
        if len(chosen) == size:
            break
    return sorted(chosen)


def fold(text: str) -> str:
    """Lowercase without accents, keeping length, so offsets map back to the original."""
    return "".join(
        unicodedata.normalize("NFKD", c)[0] if unicodedata.normalize("NFKD", c) else c
        for c in text.lower()
    )


def hits(pages: list[str], keywords: list[str], *, limit: int = 8, width: int = 160) -> list[Hit]:
    """Passages around each keyword, at most one per page, in page order."""
    found: list[Hit] = []
    pattern = re.compile("|".join(re.escape(k) for k in keywords))
    for number, text in enumerate(pages, 1):
        match = pattern.search(fold(text))
        if match:
            start, end = max(0, match.start() - width), match.end() + width
            snippet = " ".join(text[start:end].split())
            found.append(Hit(number, snippet))
        if len(found) == limit:
            break
    return found


def parse_answer(field: str, text: str) -> tuple[bool, Any]:
    """(valid, value) for what the annotator typed; value None means not stated."""
    text = text.strip().lower().replace(",", ".")
    if field == "site_visit":
        options: dict[str, Any] = {"m": "mandatory", "o": "optional", "n": None}
        return (text in options, options.get(text))
    if field == "consortium_allowed":
        options = {"y": True, "n": False, "-": None}
        return (text in options, options.get(text))
    if text == "n":
        return True, None
    try:
        number = float(text)
    except ValueError:
        return False, None
    if field == "contract_term_months":
        return (number.is_integer() and number > 0, int(number))
    return (0 < number <= 100, number)


def parse_value(field: str, text: str) -> Any:
    """A label typed during review: JSON (5, true, null, "open") or a bare word."""
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = text
    if field == "proposal_deadline" and isinstance(value, str):
        return value[:16]
    return value
