"""Download tenders and their editais from PNCP into the local database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from radar.archive import find_edital
from radar.pncp import Json, PncpClient, pick_edital


@dataclass
class FetchResult:
    stored: int = 0
    skipped: int = 0
    already_known: int = 0


def fetch(
    client: PncpClient,
    conn: sqlite3.Connection,
    pdf_dir: Path,
    start: date,
    end: date,
    *,
    uf: str | None = None,
    limit: int | None = None,
) -> FetchResult:
    """Store every new tender in the range; stop after `limit` editais were stored.

    Tenders already in the database are not downloaded again, so an interrupted run
    can simply be repeated.
    """
    result = FetchResult()
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for tender in client.search(start, end, uf=uf):
        if limit is not None and result.stored >= limit:
            break
        control_number = tender["numeroControlePNCP"]
        known = conn.execute(
            "SELECT 1 FROM notices WHERE control_number = ?", (control_number,)
        ).fetchone()
        if known:
            result.already_known += 1
            continue

        if _store(client, conn, pdf_dir, tender):
            result.stored += 1
        else:
            result.skipped += 1
    return result


def _store(client: PncpClient, conn: sqlite3.Connection, pdf_dir: Path, tender: Json) -> bool:
    cnpj = tender["orgaoEntidade"]["cnpj"]
    year = tender["anoCompra"]
    sequence = tender["sequencialCompra"]

    items = client.items(cnpj, year, sequence)
    edital = pick_edital(client.documents(cnpj, year, sequence))

    sha256 = title = skip_reason = None
    if edital is None:
        skip_reason = "no active edital"
    else:
        found = find_edital(edital["titulo"], client.download(edital["url"]))
        if isinstance(found, str):
            skip_reason = found
        else:
            sha256 = hashlib.sha256(found.pdf).hexdigest()
            title = found.title
            (pdf_dir / f"{sha256}.pdf").write_bytes(found.pdf)

    with conn:
        conn.execute(
            """
            INSERT INTO notices (control_number, cnpj, year, sequence, published_at,
                                 metadata, items, pdf_sha256, pdf_title, skip_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tender["numeroControlePNCP"],
                cnpj,
                year,
                sequence,
                tender["dataPublicacaoPncp"],
                json.dumps(tender, ensure_ascii=False),
                json.dumps(items, ensure_ascii=False),
                sha256,
                title,
                skip_reason,
            ),
        )
    return skip_reason is None
