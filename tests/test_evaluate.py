from __future__ import annotations

import json

import pytest

from radar import db
from radar.evaluate import evaluate, ground, outcome, to_markdown
from radar.labels import Label, expected, silver
from tests.helpers import fixture

METADATA = {
    "valorTotalEstimado": 2812281.48,
    "dataEncerramentoProposta": "2026-11-05T10:00:00",
    "modoDisputaNome": "Aberto",
    "srp": True,
}
ITEMS = [
    {"criterioJulgamentoNome": "Maior desconto", "tipoBeneficio": 1, "orcamentoSigiloso": False},
    {"criterioJulgamentoNome": "Maior desconto", "tipoBeneficio": 4, "orcamentoSigiloso": False},
]
STATES_BOTH = "Valor total estimado: R$ 2.812.281,48. Sessão em 05/11/2026 às 10h."


def values(labels: dict[str, Label]) -> dict:
    return {name: label.value for name, label in labels.items()}


def test_silver_labels_from_the_api():
    assert values(silver(METADATA, ITEMS, STATES_BOTH)) == {
        "estimated_value": 2812281.48,
        "proposal_deadline": "2026-11-05T10:00",
        "dispute_mode": "open",
        "price_registration": True,
        "award_criterion": "highest_discount",
        "small_business": "partial",
    }


def test_value_and_deadline_count_only_when_the_edital_states_them():
    labels = silver(METADATA, ITEMS, "Dia XX/XX/XXXX às XXh. Valor: R$ 2.812.241,48")
    assert "estimated_value" not in labels
    assert "proposal_deadline" not in labels
    # Written without thousands separators still counts.
    assert "estimated_value" in silver(METADATA, ITEMS, "R$ 2812281,48")


def test_confidential_budgets_and_ambiguous_items_get_no_label():
    secret = [{**item, "orcamentoSigiloso": True} for item in ITEMS]
    assert "estimated_value" not in silver(METADATA, secret, STATES_BOTH)

    mixed = [ITEMS[0], {**ITEMS[1], "criterioJulgamentoNome": "Menor preço"}]
    assert "award_criterion" not in silver(METADATA, mixed, STATES_BOTH)

    subcontracting = [{**ITEMS[0], "tipoBeneficio": 2}]
    assert "small_business" not in silver(METADATA, subcontracting, STATES_BOTH)


@pytest.mark.parametrize(
    ("benefits", "expected_value"),
    [({1}, "exclusive"), ({1, 4}, "partial"), ({3, 5}, "partial"), ({4, 5}, "none")],
)
def test_small_business_benefit(benefits, expected_value):
    items = [{**ITEMS[0], "tipoBeneficio": b} for b in benefits]
    assert silver(METADATA, items, STATES_BOTH)["small_business"].value == expected_value


def test_gold_and_overrides_win_over_silver(tmp_path):
    number = "42441758000105-1-000187/2026"
    (tmp_path / "overrides.jsonl").write_text(
        json.dumps({"control_number": number, "field": "dispute_mode", "value": "open_closed"})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "gold.jsonl").write_text(
        json.dumps(
            {
                "control_number": number,
                "fields": {"site_visit": {"value": None, "page": None}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    labels = expected(number, METADATA, ITEMS, [STATES_BOTH], tmp_path)
    assert labels["dispute_mode"] == Label("open_closed", "override")
    assert labels["site_visit"] == Label(None, "gold")
    assert labels["price_registration"].source == "silver"


@pytest.mark.parametrize(
    ("field", "predicted", "label", "result"),
    [
        ("estimated_value", 2812281.481, 2812281.48, "correct"),
        ("estimated_value", 2812241.48, 2812281.48, "wrong"),
        ("proposal_deadline", "2026-11-05T10:00:00", "2026-11-05T10:00", "correct"),
        ("proposal_deadline", "2026-11-05T10:00", "2026-11-05T09:59", "correct"),
        ("proposal_deadline", "2026-11-05T09:30", "2026-11-05T14:00", "wrong"),
        ("proposal_deadline", "05/11/2026", "2026-11-05T10:00", "wrong"),
        ("site_visit", None, "mandatory", "missed"),
        ("site_visit", "optional", None, "invented"),
        ("consortium_allowed", None, None, "correct"),
        ("consortium_allowed", False, None, "invented"),
    ],
)
def test_outcomes(field, predicted, label, result):
    assert outcome(field, predicted, Label(label, "gold")) == result


PAGES = ["Edital 49/2026", "9.7. A vistoria é facul-\ntativa.\nO valor é R$  2.812.241,48."]


@pytest.mark.parametrize(
    ("quote", "page", "result"),
    [
        ("A vistoria é facultativa.", 2, "on_page"),  # line-break hyphenation undone
        ("o valor é r$ 2.812.241,48", 2, "on_page"),  # case and spacing ignored
        ("Edital 49/2026", 2, "other_page"),
        ("A vistoria é obrigatória.", 2, "not_found"),
        ("Edital 49/2026", 7, "other_page"),
        ("   ", 1, "not_found"),
    ],
)
def test_grounding(quote, page, result):
    assert ground(quote, page, PAGES) == result


def test_evaluate_a_stored_run(tmp_path):
    conn = db.connect(":memory:")
    tender = fixture("tender.json")
    number = tender["numeroControlePNCP"]
    conn.execute(
        "INSERT INTO notices (control_number, cnpj, year, sequence, published_at, metadata, "
        "items, pdf_sha256) VALUES (?, '1', 2026, 1, '2026-10-20', ?, ?, 'abc')",
        (number, json.dumps(METADATA), json.dumps(ITEMS)),
    )
    conn.executemany(
        "INSERT INTO pages VALUES (?, ?, ?)", [(number, 1, STATES_BOTH), (number, 2, "Fim.")]
    )
    empty = {"value": None, "evidence": None}
    output = {
        "estimated_value": {
            "value": 2812281.48,
            "evidence": {"page": 1, "quote": "Valor total estimado: R$ 2.812.281,48"},
        },
        "proposal_deadline": empty,
        "award_criterion": {
            "value": "lowest_price",
            "evidence": {"page": 2, "quote": "menor preço"},
        },
        "dispute_mode": empty,
        "price_registration": empty,
        "small_business": empty,
        "site_visit": empty,
        "performance_guarantee_pct": empty,
        "contract_term_months": empty,
        "consortium_allowed": empty,
    }
    conn.execute(
        "INSERT INTO runs (id, model, strategy, prompt_version) VALUES (1, 'm', 'full', '1')"
    )
    conn.execute(
        "INSERT INTO predictions VALUES (1, ?, ?, NULL, 1, 1000, 100, 2000, 0, '[]')",
        (number, json.dumps(output)),
    )

    report = evaluate(conn, 1, tmp_path)

    assert report.fields["estimated_value"].outcomes == {"correct": 1}
    assert report.fields["award_criterion"].outcomes == {"wrong": 1}
    assert report.fields["proposal_deadline"].outcomes == {"missed": 1}
    assert report.grounding == {"on_page": 1, "not_found": 1}
    markdown = to_markdown(report)
    assert "| `estimated_value` | 1 | 100% | 100% |" in markdown
    # Six labels (dispute mode, SRP and small business are missed too), one correct.
    assert "Overall: 17% of 6 labelled fields." in markdown
