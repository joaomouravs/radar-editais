"""Turn each stored edital into page-by-page text. Pages are the unit of evidence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pypdfium2 as pdfium


def pdf_pages(data: bytes) -> list[str]:
    """The text of every page, in order, with Unix line endings."""
    pdf = pdfium.PdfDocument(data)
    try:
        pages: list[str] = []
        for page in pdf:
            textpage = page.get_textpage()
            pages.append(textpage.get_text_bounded().replace("\r\n", "\n"))
            textpage.close()
            page.close()
        return pages
    finally:
        pdf.close()


def parse_pending(conn: sqlite3.Connection, pdf_dir: Path) -> int:
    """Parse every stored edital that has no pages yet; returns how many were parsed."""
    pending = conn.execute(
        """
        SELECT control_number, pdf_sha256 FROM notices
        WHERE pdf_sha256 IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM pages p WHERE p.control_number = notices.control_number)
        """
    ).fetchall()
    for row in pending:
        pages = pdf_pages((pdf_dir / f"{row['pdf_sha256']}.pdf").read_bytes())
        with conn:
            conn.executemany(
                "INSERT INTO pages (control_number, page, text) VALUES (?, ?, ?)",
                [(row["control_number"], n, text) for n, text in enumerate(pages, start=1)],
            )
    return len(pending)
