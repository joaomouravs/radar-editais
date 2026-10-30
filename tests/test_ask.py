from __future__ import annotations

import json
from itertools import pairwise

from radar.ask import Answer, ask, chunks, rank, select, words
from tests.test_extract import FakeProvider, client

FILLER = "Disposições gerais sobre o procedimento e a sessão pública. " * 30

PAGES = [
    FILLER,
    "9.19. Qualificação técnica: atestado de capacidade técnica de serviços compatíveis "
    "por período não inferior a 12 (doze) meses. " + FILLER,
    FILLER + " A vistoria é facultativa. " + FILLER,
]

QUESTION = "Quais atestados de capacidade técnica são exigidos?"


def test_words_fold_accents_and_drop_stopwords():
    assert words("Quais atestados de capacidade técnica são exigidos no edital?") == [
        "atestados",
        "capacidade",
        "tecnica",
        "sao",
        "exigidos",
    ]


def test_chunks_cover_every_page_without_gaps():
    spans = chunks(PAGES, size=200)
    for number, text in enumerate(PAGES, 1):
        on_page = [s for s in spans if s.page == number]
        assert on_page[0].start == 0 and on_page[-1].end == len(text)
        assert all(a.end == b.start for a, b in pairwise(on_page))
        assert all(s.end - s.start <= 200 for s in on_page)


def test_rank_puts_the_passage_with_the_question_words_first():
    best = rank(PAGES, QUESTION)[0]
    assert best.page == 2
    assert "atestado de capacidade" in PAGES[1][best.start : best.end]


def test_select_respects_the_budget():
    spans = select(PAGES, "vistoria atestado", budget=2_500)
    assert sum(s.end - s.start for s in spans) <= 2_500
    assert {s.page for s in spans} >= {2, 3}


def answer(**fields) -> str:
    return Answer(
        answerable=fields.get("answerable", True),
        answer=fields.get("answer", "Atestado de serviços compatíveis por 12 meses."),
        citations=fields.get("citations", []),
    ).model_dump_json()


def test_citations_are_checked_against_their_pages(tmp_path):
    reply_json = answer(
        citations=[
            {"page": 2, "quote": "atestado de capacidade técnica de serviços compatíveis"},
            {"page": 3, "quote": "atestado de capacidade técnica de serviços compatíveis"},
            {"page": 2, "quote": "atestado emitido pelo CREA"},
        ]
    )
    provider = FakeProvider(reply_json)

    reply = ask(client(provider, tmp_path), _model(), PAGES, QUESTION)

    assert [c.found for c in reply.checks] == ["on_page", "other_page", "not_found"]
    assert 2 in reply.pages_sent
    sent = provider.requests[0]
    assert sent["messages"][1]["content"].endswith(f"Question: {QUESTION}")
    assert sent["response_format"]["json_schema"]["schema"]["required"] == [
        "answerable",
        "answer",
        "citations",
    ]


def test_an_unanswerable_question_is_reported_not_answered(tmp_path):
    provider = FakeProvider(answer(answerable=False, answer="O trecho não trata disso."))

    reply = ask(client(provider, tmp_path), _model(), PAGES, "Qual é a vistoria exigida?")

    assert reply.answer is not None and reply.answer.answerable is False
    assert reply.checks == []


def test_no_matching_passage_means_no_model_call(tmp_path):
    provider = FakeProvider()

    reply = ask(client(provider, tmp_path), _model(), PAGES, "xyzzy?")

    assert reply.answer is None and provider.requests == []


def test_an_invalid_answer_is_an_error_not_a_crash(tmp_path):
    reply = ask(
        client(FakeProvider(json.dumps({"answer": 3})), tmp_path), _model(), PAGES, QUESTION
    )
    assert reply.answer is None and reply.error


def _model():
    from radar.llm import Model

    return Model.parse("gemini:gemini-3.5-flash-lite")
