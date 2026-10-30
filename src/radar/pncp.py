"""Client for the public PNCP APIs. None of the endpoints used here need authentication."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import httpx

BASE_URL = "https://pncp.gov.br"
USER_AGENT = "radar-editais (+https://github.com/joaomouravs/radar-editais)"

PREGAO_ELETRONICO = 6
EDITAL_DOCUMENT_TYPE = 2

# The search endpoint answers 400 to any page size above 50.
SEARCH_PAGE_SIZE = 50
ITEMS_PAGE_SIZE = 50

Json = dict[str, Any]


def new_http_client() -> httpx.Client:
    # Edital PDFs reach a few megabytes and the portal is slow at peak hours.
    return httpx.Client(
        base_url=BASE_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
    )


class PncpClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._retries = retries
        self._sleep = sleep

    def search(
        self,
        start: date,
        end: date,
        *,
        modality: int = PREGAO_ELETRONICO,
        uf: str | None = None,
    ) -> Iterator[Json]:
        """Tenders published between start and end (inclusive), oldest page first."""
        page = 1
        while True:
            params: dict[str, str | int] = {
                "dataInicial": start.strftime("%Y%m%d"),
                "dataFinal": end.strftime("%Y%m%d"),
                "codigoModalidadeContratacao": modality,
                "pagina": page,
                "tamanhoPagina": SEARCH_PAGE_SIZE,
            }
            if uf:
                params["uf"] = uf
            resp = self._get("/api/consulta/v1/contratacoes/publicacao", params)
            # No results at all comes back as 204 with an empty body, not as an empty page.
            if resp.status_code == 204:
                return
            body = resp.json()
            yield from body["data"]
            if body["paginasRestantes"] == 0:
                return
            page += 1

    def documents(self, cnpj: str, year: int, sequence: int) -> list[Json]:
        resp = self._get(f"/api/pncp/v1/orgaos/{cnpj}/compras/{year}/{sequence}/arquivos")
        return resp.json()

    def items(self, cnpj: str, year: int, sequence: int) -> list[Json]:
        # This endpoint has no page count; a page past the end is an empty list.
        items: list[Json] = []
        page = 1
        while True:
            resp = self._get(
                f"/api/pncp/v1/orgaos/{cnpj}/compras/{year}/{sequence}/itens",
                {"pagina": page, "tamanhoPagina": ITEMS_PAGE_SIZE},
            )
            batch = resp.json()
            items.extend(batch)
            if len(batch) < ITEMS_PAGE_SIZE:
                return items
            page += 1

    def download(self, url: str) -> bytes:
        return self._get(url).content

    def _get(self, url: str, params: dict[str, str | int] | None = None) -> httpx.Response:
        """GET with retries on network errors, 429 and 5xx, backing off 1s, 2s, 4s..."""
        attempt = 0
        while True:
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError:
                if attempt == self._retries:
                    raise
            else:
                retryable = resp.status_code == 429 or resp.status_code >= 500
                if not retryable or attempt == self._retries:
                    resp.raise_for_status()
                    return resp
            self._sleep(2.0**attempt)
            attempt += 1


def pick_edital(documents: list[Json]) -> Json | None:
    """The edital to read, or None if the tender has none.

    A republished edital is uploaded as a new document, and the old file may stay
    active, so the most recent active one wins.
    """
    editais = [
        d for d in documents if d["tipoDocumentoId"] == EDITAL_DOCUMENT_TYPE and d["statusAtivo"]
    ]
    return max(editais, key=lambda d: d["sequencialDocumento"], default=None)
