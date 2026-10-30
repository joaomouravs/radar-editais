"""The local SQLite database: one row per tender and one row per page of its edital."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    -- numeroControlePNCP, e.g. 42441758000105-1-000187/2026
    control_number TEXT PRIMARY KEY,
    cnpj           TEXT    NOT NULL,
    year           INTEGER NOT NULL,
    sequence       INTEGER NOT NULL,
    published_at   TEXT    NOT NULL,
    -- Raw API responses, kept whole: they are the silver labels.
    metadata       TEXT    NOT NULL,
    items          TEXT    NOT NULL,
    -- The edital is stored at pdfs/<sha256>.pdf. Both are NULL when it was skipped.
    pdf_sha256     TEXT,
    pdf_title      TEXT,
    skip_reason    TEXT,
    fetched_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK ((pdf_sha256 IS NULL) <> (skip_reason IS NULL))
);

CREATE TABLE IF NOT EXISTS pages (
    control_number TEXT    NOT NULL REFERENCES notices (control_number),
    page           INTEGER NOT NULL,  -- 1-based, as printed in citations
    text           TEXT    NOT NULL,
    PRIMARY KEY (control_number, page)
);

-- One run is one model with one prompt version and context strategy over many notices.
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY,
    model          TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS predictions (
    run_id         INTEGER NOT NULL REFERENCES runs (id),
    control_number TEXT    NOT NULL REFERENCES notices (control_number),
    output         TEXT,              -- validated NoticeExtraction, NULL on failure
    error          TEXT,
    attempts       INTEGER NOT NULL,
    tokens_in      INTEGER NOT NULL,
    tokens_out     INTEGER NOT NULL,
    latency_ms     INTEGER NOT NULL,  -- as first measured, even when served from cache
    cached         INTEGER NOT NULL,
    raw            TEXT    NOT NULL,  -- every answer the model gave, as a JSON list
    PRIMARY KEY (run_id, control_number)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
