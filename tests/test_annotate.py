from __future__ import annotations

import pytest

from radar import db
from radar.annotate import fold, hits, parse_answer, parse_value, sample


def test_fold_keeps_offsets():
    text = "Visita Técnica e CONSÓRCIO"
    assert fold(text) == "visita tecnica e consorcio"
    assert len(fold(text)) == len(text)


def test_hits_show_one_passage_per_page_with_accents_intact():
    pages = [
        "Nada aqui.",
        "9.7. A vistoria é facultativa. Outra vistoria depois.",
        "Não há VISITA TÉCNICA obrigatória.",
    ]
    found = hits(pages, ["vistoria", "visita tecnica"], width=10)
    assert [h.page for h in found] == [2, 3]
    assert "vistoria é" in found[0].snippet
    assert "VISITA TÉCNICA" in found[1].snippet


@pytest.mark.parametrize(
    ("field", "typed", "expected"),
    [
        ("site_visit", "M", (True, "mandatory")),
        ("site_visit", "n", (True, None)),
        ("site_visit", "maybe", (False, None)),
        ("consortium_allowed", "y", (True, True)),
        ("consortium_allowed", "n", (True, False)),
        ("consortium_allowed", "-", (True, None)),
        ("performance_guarantee_pct", "5", (True, 5.0)),
        ("performance_guarantee_pct", "2,5", (True, 2.5)),
        ("performance_guarantee_pct", "n", (True, None)),
        ("performance_guarantee_pct", "500", (False, 500.0)),
        ("contract_term_months", "12", (True, 12)),
        ("contract_term_months", "1.5", (False, 1)),
        ("contract_term_months", "doze", (False, None)),
    ],
)
def test_parse_answer(field, typed, expected):
    assert parse_answer(field, typed) == expected


def test_sample_is_reproducible_and_limits_each_body():
    conn = db.connect(":memory:")
    for body in range(4):
        for n in range(5):
            number = f"{body}-1-{n:06d}/2026"
            conn.execute(
                "INSERT INTO notices (control_number, cnpj, year, sequence, published_at, "
                "metadata, items, pdf_sha256) VALUES (?, ?, 2026, ?, '', '{}', '[]', 'x')",
                (number, str(body), n),
            )
            conn.execute("INSERT INTO pages VALUES (?, 1, 'texto')", (number,))

    chosen = sample(conn, 10, per_body=2)

    assert chosen == sample(conn, 10, per_body=2)
    assert len(chosen) == 8  # four bodies, two each, fewer than asked
    assert all(sum(c.startswith(f"{b}-") for c in chosen) == 2 for b in range(4))


@pytest.mark.parametrize(
    ("field", "typed", "expected"),
    [
        ("estimated_value", "2639799.36", 2639799.36),
        ("price_registration", "false", False),
        ("small_business", "exclusive", "exclusive"),
        ("small_business", '"partial"', "partial"),
        ("site_visit", "null", None),
        ("proposal_deadline", "2026-11-05T10:00:00", "2026-11-05T10:00"),
    ],
)
def test_parse_value(field, typed, expected):
    assert parse_value(field, typed) == expected
