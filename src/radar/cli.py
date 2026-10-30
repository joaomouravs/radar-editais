from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer

from radar import db
from radar.extract import extract as extract_notice
from radar.extract import pages_of, save, start_run
from radar.fetch import fetch as fetch_notices
from radar.llm import ChatClient, Model
from radar.parse import parse_pending
from radar.pncp import PncpClient, new_http_client

app = typer.Typer(no_args_is_help=True, add_completion=False)

DataDir = Annotated[Path, typer.Option(help="Where the database and the PDFs live.")]


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


@app.command()
def fetch(
    start: Annotated[str, typer.Option("--from", help="First publication day, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option("--to", help="Last publication day, YYYY-MM-DD.")],
    uf: Annotated[str | None, typer.Option(help="State, e.g. RJ.")] = None,
    limit: Annotated[int | None, typer.Option(help="Stop after this many editais.")] = None,
    data_dir: DataDir = Path("data"),
) -> None:
    """Download electronic reverse auctions (pregão eletrônico) and their editais."""
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(data_dir / "radar.db")
    with new_http_client() as http:
        result = fetch_notices(
            PncpClient(http),
            conn,
            data_dir / "pdfs",
            _date(start),
            _date(end),
            uf=uf,
            limit=limit,
        )
    typer.echo(
        f"stored {result.stored}, skipped {result.skipped}, already known {result.already_known}"
    )


@app.command()
def parse(data_dir: DataDir = Path("data")) -> None:
    """Extract the text of every edital that was not parsed yet."""
    conn = db.connect(data_dir / "radar.db")
    typer.echo(f"parsed {parse_pending(conn, data_dir / 'pdfs')} editais")


@app.command()
def stats(data_dir: DataDir = Path("data")) -> None:
    """What the database holds."""
    conn = db.connect(data_dir / "radar.db")
    row = conn.execute(
        """
        SELECT count(*) AS notices,
               count(pdf_sha256) AS editais,
               (SELECT count(DISTINCT control_number) FROM pages) AS parsed,
               (SELECT count(*) FROM pages) AS pages
        FROM notices
        """
    ).fetchone()
    typer.echo(
        f"notices {row['notices']}, editais {row['editais']}, "
        f"parsed {row['parsed']}, pages {row['pages']}"
    )
    for reason in conn.execute(
        "SELECT skip_reason, count(*) AS n FROM notices WHERE skip_reason IS NOT NULL "
        "GROUP BY skip_reason ORDER BY n DESC"
    ):
        typer.echo(f"  skipped ({reason['skip_reason']}): {reason['n']}")


@app.command()
def extract(
    model: Annotated[
        str, typer.Option(help="provider:model[@reasoning], e.g. groq:openai/gpt-oss-120b@low.")
    ],
    notice: Annotated[
        list[str] | None, typer.Option(help="Control number; repeat for several.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="At most this many notices.")] = None,
    data_dir: DataDir = Path("data"),
) -> None:
    """Run the extraction over parsed notices and record it as a new run."""
    conn = db.connect(data_dir / "radar.db")
    chosen = Model.parse(model)
    # Sorted by control number so that the same --limit always picks the same notices.
    numbers = notice or [
        row["control_number"]
        for row in conn.execute("SELECT DISTINCT control_number FROM pages ORDER BY control_number")
    ]
    numbers = numbers[:limit] if limit else numbers
    run_id = start_run(conn, chosen, strategy="full")
    typer.echo(f"run {run_id}: {chosen.id} over {len(numbers)} notices")

    ok = 0
    # Thinking models answering about a 300-page edital can take minutes.
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as http:
        client = ChatClient(http, data_dir / "cache")
        for number in numbers:
            pages = pages_of(conn, number)
            result = extract_notice(client, chosen, pages)
            save(conn, run_id, number, result)
            ok += result.output is not None
            status = "ok" if result.output else f"FAILED: {(result.error or '')[:120]}"
            typer.echo(
                f"  {number}  {len(pages):>3} pages  {result.tokens_in:>7} in "
                f"{result.tokens_out:>5} out  {result.latency_ms / 1000:>6.1f}s"
                f"{'  cached' if result.cached else ''}  {status}"
            )
    typer.echo(f"run {run_id}: {ok}/{len(numbers)} valid")
