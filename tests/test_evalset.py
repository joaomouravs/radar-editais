from __future__ import annotations

import json

from radar import db
from radar.evalset import (
    build_report,
    export_dataset,
    export_run,
    import_dataset,
    load_dataset,
    load_runs,
)
from radar.evaluate import Prediction
from tests.helpers import fixture
from tests.test_evaluate import ITEMS, METADATA, STATES_BOTH

NUMBER = "42441758000105-1-000187/2026"
EMPTY = {"value": None, "evidence": None}


def database():
    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO notices (control_number, cnpj, year, sequence, published_at, metadata, "
        "items, pdf_sha256) VALUES (?, '1', 2026, 1, '2026-10-20', ?, ?, 'abc')",
        (NUMBER, json.dumps({**fixture("tender.json"), **METADATA}), json.dumps(ITEMS)),
    )
    conn.executemany(
        "INSERT INTO pages VALUES (?, ?, ?)", [(NUMBER, 1, STATES_BOTH), (NUMBER, 2, "Fim.")]
    )
    output: dict[str, object] = {
        name: EMPTY
        for name in [
            "proposal_deadline",
            "award_criterion",
            "dispute_mode",
            "price_registration",
            "small_business",
            "site_visit",
            "performance_guarantee_pct",
            "contract_term_months",
            "consortium_allowed",
        ]
    }
    output["estimated_value"] = {
        "value": 2812281.48,
        "evidence": {"page": 1, "quote": "Valor total estimado: R$ 2.812.281,48"},
    }
    conn.execute(
        "INSERT INTO runs (id, model, strategy, prompt_version) "
        "VALUES (7, 'groq:m', 'retrieve', '3')"
    )
    conn.execute(
        "INSERT INTO predictions VALUES (7, ?, ?, NULL, 1, 5000, 400, 1500, 0, '[]')",
        (NUMBER, json.dumps(output)),
    )
    return conn


def test_dataset_round_trip_is_byte_stable(tmp_path):
    conn = database()
    path = tmp_path / "dataset.jsonl.gz"

    export_dataset(conn, [NUMBER], path)
    first = path.read_bytes()
    export_dataset(conn, [NUMBER], path)

    assert path.read_bytes() == first
    notice = load_dataset(path)[NUMBER]
    assert notice.pages == [STATES_BOTH, "Fim."]
    assert notice.metadata["srp"] is True


def test_runs_round_trip(tmp_path):
    export_run(database(), 7, "groq-m-retrieve", tmp_path)

    [(name, meta, predictions)] = load_runs(tmp_path)

    assert name == "groq-m-retrieve"
    assert meta == {"model": "groq:m", "strategy": "retrieve", "prompt_version": "3"}
    assert predictions[0] == Prediction(NUMBER, predictions[0].output, 5000, 400, 1500, None)


def test_report_scores_the_files_alone(tmp_path):
    conn = database()
    export_dataset(conn, [NUMBER], tmp_path / "dataset.jsonl.gz")
    export_run(conn, 7, "groq-m-retrieve", tmp_path / "runs")
    conn.close()

    text = build_report(tmp_path / "dataset.jsonl.gz", tmp_path / "runs", tmp_path)

    # The value is right; the five other silver fields are missed: 1 of 6.
    assert "| groq-m-retrieve | `groq:m` | retrieve | 1/1 | 17% | - | 100% | 5,000 |" in text
    assert text == build_report(tmp_path / "dataset.jsonl.gz", tmp_path / "runs", tmp_path)


def test_import_dataset_rebuilds_what_extract_reads(tmp_path):
    path = tmp_path / "dataset.jsonl.gz"
    export_dataset(database(), [NUMBER], path)
    fresh = db.connect(":memory:")

    assert import_dataset(fresh, path) == 1
    assert import_dataset(fresh, path) == 1  # running it twice changes nothing

    pages = [r["text"] for r in fresh.execute("SELECT text FROM pages ORDER BY page")]
    assert pages == [STATES_BOTH, "Fim."]
    assert fresh.execute("SELECT cnpj FROM notices").fetchone()["cnpj"] == "42441758000105"
