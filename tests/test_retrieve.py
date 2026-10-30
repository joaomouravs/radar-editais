from __future__ import annotations

from radar import retrieve
from radar.retrieve import Span, render, select

FILLER = "Lorem ipsum dolor sit amet. " * 40  # about 1,100 characters

PAGES = [
    "EDITAL 49/2026. Valor estimado: R$ 1.000,00. " + FILLER,
    FILLER + "9.7. A VISTORIA é facultativa. " + FILLER,
    FILLER,
    FILLER + "É vedada a participação de consórcio. " + FILLER,
]


def test_the_preamble_and_every_field_with_a_match_are_included():
    spans = select(PAGES)

    assert spans[0] == Span(1, 0, retrieve.PREAMBLE_CHARS) or spans[0].start == 0
    text = render(PAGES, spans)
    assert "Valor estimado: R$ 1.000,00" in text
    assert "A VISTORIA é facultativa." in text
    assert "vedada a participação de consórcio" in text
    assert "<page 3>" not in text  # no keyword there


def test_the_budget_is_respected_and_shared_between_fields(monkeypatch):
    monkeypatch.setattr(retrieve, "PREAMBLE_CHARS", 100)
    monkeypatch.setattr(retrieve, "WINDOW_CHARS", 50)
    many = ["vistoria " + FILLER + " consorcio " + FILLER for _ in range(10)]

    spans = select(many, budget=600)

    assert sum(s.end - s.start for s in spans) <= 600
    text = render(many, spans).lower()
    # Round robin: consortium gets a window before site visit gets a second one.
    assert "vistoria" in text and "consorcio" in text


def test_overlapping_windows_on_a_page_are_merged():
    page = "vistoria facultativa e consórcio vedado " + FILLER
    spans = select([page])
    assert len(spans) == 1 and spans[0].start == 0


def test_render_marks_gaps_and_keeps_page_numbers():
    pages = ["abcdefghij", "0123456789"]
    text = render(pages, [Span(2, 0, 3), Span(2, 6, 9), Span(1, 0, 2)])
    assert text == "<page 1>\nab\n</page 1>\n<page 2>\n012\n[...]\n678\n</page 2>"
