from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer

from radar import db
from radar.fetch import fetch as fetch_notices
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
