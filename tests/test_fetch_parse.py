from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from radar import db
from radar.fetch import fetch
from radar.parse import parse_pending, pdf_pages
from radar.pncp import PncpClient
from tests.helpers import fixture, make_pdf

EDITAL_PAGES = ["Edital 49/2026 - Pregão Eletrônico", "Da visita técnica: é facultativa."]


class FakePortal:
    """Serves one tender, its documents and items, and its edital file."""

    def __init__(self, documents: list[dict] | None = None, edital: bytes | None = None):
        self.tender = fixture("tender.json")
        self.documents = fixture("documents.json") if documents is None else documents
        self.items = fixture("items.json")
        self.edital = make_pdf(EDITAL_PAGES) if edital is None else edital
        self.downloads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/contratacoes/publicacao"):
            return httpx.Response(200, json={"data": [self.tender], "paginasRestantes": 0})
        if path.endswith("/arquivos"):
            return httpx.Response(200, json=self.documents)
        if path.endswith("/itens"):
            return httpx.Response(200, json=self.items)
        if path.endswith("/arquivos/2"):
            self.downloads += 1
            return httpx.Response(200, content=self.edital)
        return httpx.Response(404)


def run_fetch(portal: FakePortal, conn, pdf_dir: Path):
    http = httpx.Client(base_url="https://pncp.test", transport=httpx.MockTransport(portal))
    return fetch(PncpClient(http), conn, pdf_dir, date(2026, 10, 20), date(2026, 10, 20))


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_fetch_stores_metadata_items_and_the_edital(conn, tmp_path):
    portal = FakePortal()

    result = run_fetch(portal, conn, tmp_path)

    assert (result.stored, result.skipped) == (1, 0)
    row = conn.execute("SELECT * FROM notices").fetchone()
    assert row["control_number"] == "42441758000105-1-000187/2026"
    assert json.loads(row["metadata"])["srp"] is True
    assert len(json.loads(row["items"])) == 2
    assert row["pdf_title"] == "Edital 49.2026.pdf"
    assert row["pdf_sha256"] == hashlib.sha256(portal.edital).hexdigest()
    assert (tmp_path / f"{row['pdf_sha256']}.pdf").read_bytes() == portal.edital


def test_fetch_does_not_download_known_tenders_again(conn, tmp_path):
    portal = FakePortal()
    run_fetch(portal, conn, tmp_path)

    result = run_fetch(portal, conn, tmp_path)

    assert (result.stored, result.already_known) == (0, 1)
    assert portal.downloads == 1


@pytest.mark.parametrize(
    ("portal", "reason"),
    [
        (FakePortal(documents=[]), "no active edital"),
        (FakePortal(edital=b"7z\xbc\xaf'\x1c"), "edital is not a PDF or a ZIP"),
    ],
)
def test_fetch_records_why_a_tender_was_skipped(conn, tmp_path, portal, reason):
    result = run_fetch(portal, conn, tmp_path)

    assert result.skipped == 1
    row = conn.execute("SELECT pdf_sha256, skip_reason FROM notices").fetchone()
    assert (row["pdf_sha256"], row["skip_reason"]) == (None, reason)


def test_pdf_pages_keeps_order_and_accents():
    assert [p.strip() for p in pdf_pages(make_pdf(EDITAL_PAGES))] == EDITAL_PAGES


def test_parse_pending_stores_pages_once(conn, tmp_path):
    run_fetch(FakePortal(), conn, tmp_path)

    assert parse_pending(conn, tmp_path) == 1
    assert parse_pending(conn, tmp_path) == 0
    pages = conn.execute("SELECT page, text FROM pages ORDER BY page").fetchall()
    assert [(p["page"], p["text"].strip()) for p in pages] == list(enumerate(EDITAL_PAGES, 1))
