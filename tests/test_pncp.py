from __future__ import annotations

from datetime import date

import httpx
import pytest

from radar.pncp import PncpClient, pick_edital
from tests.helpers import fixture


def client_for(handler, sleeps: list[float] | None = None) -> PncpClient:
    http = httpx.Client(base_url="https://pncp.test", transport=httpx.MockTransport(handler))
    return PncpClient(http, sleep=(sleeps if sleeps is not None else []).append)


def test_search_follows_pages_until_none_remain():
    tender = fixture("tender.json")
    pages_requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pagina"])
        pages_requested.append(page)
        assert request.url.params["tamanhoPagina"] == "50"
        assert request.url.params["dataInicial"] == "20261020"
        assert request.url.params["uf"] == "RJ"
        return httpx.Response(200, json={"data": [tender], "paginasRestantes": 2 - page})

    found = list(client_for(handler).search(date(2026, 10, 20), date(2026, 10, 21), uf="RJ"))

    assert pages_requested == [1, 2]
    assert len(found) == 2


def test_search_with_no_results_is_204():
    found = list(
        client_for(lambda _: httpx.Response(204)).search(date(2026, 10, 20), date(2026, 10, 20))
    )
    assert found == []


def test_items_stop_at_a_short_page():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pagina"])
        return httpx.Response(200, json=[{"numeroItem": n} for n in range(50 if page == 1 else 3)])

    assert len(client_for(handler).items("42441758000105", 2026, 187)) == 53


def test_retries_server_errors_with_backoff():
    statuses = iter([503, 429, 200])
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), json=[])

    assert client_for(handler, sleeps).documents("42441758000105", 2026, 187) == []
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_the_last_retry():
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502)

    with pytest.raises(httpx.HTTPStatusError):
        client_for(handler).documents("42441758000105", 2026, 187)
    assert calls == 4


def test_does_not_retry_client_errors():
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    with pytest.raises(httpx.HTTPStatusError):
        client_for(handler).documents("42441758000105", 2026, 187)
    assert calls == 1


def test_pick_edital_ignores_other_types_and_inactive_files():
    def title(documents: list[dict]) -> str | None:
        edital = pick_edital(documents)
        return edital["titulo"] if edital else None

    documents = fixture("documents.json")
    assert title(documents) == "Edital 49.2026.pdf"

    republished = {**documents[1], "sequencialDocumento": 9, "titulo": "Edital v2.pdf"}
    withdrawn = {**documents[1], "sequencialDocumento": 10, "statusAtivo": False}
    assert title([*documents, republished, withdrawn]) == "Edital v2.pdf"

    assert pick_edital([d for d in documents if d["tipoDocumentoId"] != 2]) is None
