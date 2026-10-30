"""What is extracted from an edital. Field descriptions are sent to the model as the
JSON schema, so they are the instructions for each field and live in one place."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

# Bumped whenever the prompt or a field description changes, so runs stay comparable.
PROMPT_VERSION = "3"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(description="Number of the page the quote is on, as in <page n>.")
    quote: str = Field(
        description="The shortest verbatim passage from that page that supports the value, "
        "copied exactly, at most 300 characters."
    )

    @field_validator("page")
    @classmethod
    def page_exists(cls, page: int, info: ValidationInfo) -> int:
        pages = (info.context or {}).get("pages")
        if page < 1 or (pages is not None and page > pages):
            raise ValueError(f"page {page} does not exist; the document has {pages} pages")
        return page


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = Field(
        description="Total estimated value of the tender in reais, as a number "
        "(R$ 1.234.567,89 is 1234567.89). Null if not stated or if the budget is "
        "confidential (orçamento sigiloso)."
    )
    evidence: Evidence | None


class Deadline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | None = Field(
        description="When proposals close (encerramento do recebimento das propostas, or "
        "the opening of the public session), Brasília time, as YYYY-MM-DDTHH:MM."
    )
    evidence: Evidence | None


class AwardCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: (
        Literal[
            "lowest_price",
            "highest_discount",
            "best_technique",
            "technique_and_price",
            "highest_economic_return",
        ]
        | None
    ) = Field(
        description="Critério de julgamento: menor preço, maior desconto, melhor técnica, "
        "técnica e preço, maior retorno econômico."
    )
    evidence: Evidence | None


class DisputeMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Literal["open", "closed", "open_closed", "closed_open"] | None = Field(
        description="Modo de disputa: aberto, fechado, aberto e fechado, fechado e aberto."
    )
    evidence: Evidence | None


class Flag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: bool | None
    evidence: Evidence | None


class SmallBusiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Literal["exclusive", "partial", "none"] | None = Field(
        description="Benefit for micro and small businesses (ME/EPP, LC 123/2006): "
        "'exclusive' if every item is exclusive to them, 'partial' if only some items are "
        "exclusive or a quota is reserved (cota reservada), 'none' if there is no benefit."
    )
    evidence: Evidence | None


class SiteVisit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Literal["mandatory", "optional"] | None = Field(
        description="Vistoria or visita técnica by bidders: 'mandatory' if they must visit "
        "and cannot replace it with a declaration; 'optional' if it is facultativa or can be "
        "replaced by a declaration of knowing the site. Null if not mentioned."
    )
    evidence: Evidence | None


class Percentage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = Field(
        description="Garantia de execução contratual as a percentage of the contract value "
        "(5% is 5). Null if the edital does not require one or does not say. Ignore the "
        "garantia de proposta."
    )
    evidence: Evidence | None


class Months(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int | None = Field(
        description="For a price registration (SRP), the term of the ata de registro de "
        "preços; otherwise the initial term of the contract. In months, without extensions "
        "(one year is 12; 60 days is 2). Null if not stated, only referred to another "
        "document, or given in days that are not whole months."
    )
    evidence: Evidence | None


class NoticeExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimated_value: Money
    proposal_deadline: Deadline
    award_criterion: AwardCriterion
    dispute_mode: DisputeMode
    price_registration: Flag = Field(
        description="Whether this is a Sistema de Registro de Preços (SRP)."
    )
    small_business: SmallBusiness
    site_visit: SiteVisit
    performance_guarantee_pct: Percentage
    contract_term_months: Months
    consortium_allowed: Flag = Field(
        description="Whether companies may bid as a consortium (consórcio): false if the "
        "edital forbids it. A clause about who wrote the basic project is not about this. "
        "Null if not mentioned."
    )


FIELDS = list(NoticeExtraction.model_fields)


def strict_json_schema() -> dict[str, Any]:
    """The schema in the form strict structured output accepts.

    Providers want every property listed as required, no extra properties and no
    references, so the Pydantic schema is inlined and tightened here.
    """
    schema = NoticeExtraction.model_json_schema()
    definitions = schema.pop("$defs", {})

    def tighten(node: Any) -> Any:
        if isinstance(node, list):
            return [tighten(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            resolved = dict(definitions[node["$ref"].split("/")[-1]])
            # A description next to a $ref belongs to the field, not to the type.
            if "description" in node:
                resolved["description"] = node["description"]
            node = resolved
        node = {k: tighten(v) for k, v in node.items() if k not in ("title", "default")}
        if node.get("type") == "object":
            node["required"] = list(node.get("properties", {}))
            node["additionalProperties"] = False
        return node

    return tighten(schema)
