"""The expected answers: silver labels derived from the PNCP API, gold labels annotated
by hand, and hand-reviewed corrections where the API disagrees with the edital."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from radar.pncp import Json

ANNOTATIONS = Path("annotations")
GOLD_FIELDS = [
    "site_visit",
    "performance_guarantee_pct",
    "contract_term_months",
    "consortium_allowed",
]

_DISPUTE_MODES = {
    "Aberto": "open",
    "Fechado": "closed",
    "Aberto-Fechado": "open_closed",
    "Fechado-Aberto": "closed_open",
}
_AWARD_CRITERIA = {
    "Menor preço": "lowest_price",
    "Maior desconto": "highest_discount",
    "Melhor técnica ou conteúdo artístico": "best_technique",
    "Técnica e preço": "technique_and_price",
    "Maior retorno econômico": "highest_economic_return",
}
# tipoBeneficio of an item. Subcontracting (2) is left unlabelled: the schema has no
# value for it.
_EXCLUSIVE, _SUBCONTRACTING, _QUOTA, _NO_BENEFIT, _NOT_APPLICABLE = 1, 2, 3, 4, 5


@dataclass(frozen=True)
class Label:
    value: Any  # None means the edital does not state it; only gold labels assert that
    source: str  # "silver", "gold" or "override"


def silver(metadata: Json, items: list[Json], document: str) -> dict[str, Label]:
    """Labels the API metadata supports. A field is left out when the API is silent or
    ambiguous about it, never guessed.

    The API describes the tender, not the PDF. In the first ten notices scored, the API's
    estimated value was written in the edital only 3 times and the closing date only
    twice: the rest live in the procurement system. So those two labels count only when
    the edital spells them out, otherwise the model would be marked wrong for answering
    "not stated" correctly.
    """
    labels: dict[str, Label] = {}

    def put(field: str, value: Any) -> None:
        if value is not None:
            labels[field] = Label(value, "silver")

    # A confidential budget is published as zero or not at all.
    value = metadata.get("valorTotalEstimado")
    if value and not any(item.get("orcamentoSigiloso") for item in items):
        written = f"{value:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
        if written in document or written.replace(".", "") in document:
            put("estimated_value", value)
    if (deadline := metadata.get("dataEncerramentoProposta")) and (
        f"{deadline[8:10]}/{deadline[5:7]}/{deadline[:4]}" in document
    ):
        put("proposal_deadline", deadline[:16])
    put("dispute_mode", _DISPUTE_MODES.get(metadata.get("modoDisputaNome", "")))
    put("price_registration", metadata.get("srp"))

    criteria = {item.get("criterioJulgamentoNome") for item in items}
    if len(criteria) == 1:
        put("award_criterion", _AWARD_CRITERIA.get(criteria.pop() or ""))

    benefits = {item.get("tipoBeneficio") for item in items}
    if items and _SUBCONTRACTING not in benefits and None not in benefits:
        if benefits == {_EXCLUSIVE}:
            put("small_business", "exclusive")
        elif benefits & {_EXCLUSIVE, _QUOTA}:
            put("small_business", "partial")
        else:
            put("small_business", "none")
    return labels


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def gold(directory: Path = ANNOTATIONS) -> dict[str, dict[str, Label]]:
    """Hand annotations, one JSON object per notice in gold.jsonl."""
    return {
        entry["control_number"]: {
            field: Label(answer["value"], "gold") for field, answer in entry["fields"].items()
        }
        for entry in _read_jsonl(directory / "gold.jsonl")
    }


def overrides(directory: Path = ANNOTATIONS) -> dict[str, dict[str, Label]]:
    """Silver labels corrected after reading the edital, each with its reason."""
    found: dict[str, dict[str, Label]] = {}
    for entry in _read_jsonl(directory / "overrides.jsonl"):
        found.setdefault(entry["control_number"], {})[entry["field"]] = Label(
            entry["value"], "override"
        )
    return found


def expected(
    control_number: str,
    metadata: Json,
    items: list[Json],
    pages: list[str],
    directory: Path = ANNOTATIONS,
) -> dict[str, Label]:
    """Every label for one notice. Gold and overrides win over silver."""
    labels = silver(metadata, items, "\n".join(pages))
    labels.update(overrides(directory).get(control_number, {}))
    labels.update(gold(directory).get(control_number, {}))
    return labels
