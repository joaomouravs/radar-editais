from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer

from radar import db
from radar.annotate import KEYWORDS, QUESTIONS, parse_answer, parse_value
from radar.annotate import hits as keyword_hits
from radar.annotate import sample as choose_sample
from radar.evalset import (
    DATASET,
    REPORT,
    build_report,
    export_dataset,
    export_run,
    import_dataset,
)
from radar.evaluate import evaluate as score_run
from radar.evaluate import to_markdown
from radar.extract import STRATEGIES, pages_of, save, start_run
from radar.extract import extract as extract_notice
from radar.fetch import fetch as fetch_notices
from radar.labels import ANNOTATIONS, GOLD_FIELDS, gold
from radar.llm import ChatClient, Model
from radar.parse import parse_pending
from radar.pncp import PncpClient, new_http_client

app = typer.Typer(no_args_is_help=True, add_completion=False)

EVAL_SET = Path("annotations/eval_set.txt")

# Passages shown next to a disagreement, so the edital can be checked without the PDF.
REVIEW_KEYWORDS = {
    "estimated_value": ["valor estimado", "valor total", "valor global"],
    "proposal_deadline": ["sessao publica", "abertura", "envio de propostas"],
    "award_criterion": ["criterio de julgamento"],
    "dispute_mode": ["modo de disputa"],
    "price_registration": ["registro de precos"],
    "small_business": ["exclusiv", "cota reservada", "cota de ate"],
}

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
    eval_set: Annotated[
        bool, typer.Option("--eval-set", help="Only the notices in annotations/eval_set.txt.")
    ] = False,
    strategy: Annotated[
        str, typer.Option(help="full: the whole edital; retrieve: excerpts around keywords.")
    ] = "full",
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
    if eval_set:
        numbers = EVAL_SET.read_text(encoding="utf-8").split()
    numbers = numbers[:limit] if limit else numbers
    if strategy not in STRATEGIES:
        raise typer.BadParameter(f"strategy must be one of {', '.join(STRATEGIES)}")
    run_id = start_run(conn, chosen, strategy=strategy)
    typer.echo(f"run {run_id}: {chosen.id}, {strategy}, over {len(numbers)} notices")

    ok = 0
    # Thinking models answering about a 300-page edital can take minutes.
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as http:
        client = ChatClient(http, data_dir / "cache")
        for number in numbers:
            pages = pages_of(conn, number)
            result = extract_notice(client, chosen, pages, strategy)
            save(conn, run_id, number, result)
            ok += result.output is not None
            status = "ok" if result.output else f"FAILED: {(result.error or '')[:120]}"
            typer.echo(
                f"  {number}  {len(pages):>3} pages  {result.tokens_in:>7} in "
                f"{result.tokens_out:>5} out  {result.latency_ms / 1000:>6.1f}s"
                f"{'  cached' if result.cached else ''}  {status}"
            )
    typer.echo(f"run {run_id}: {ok}/{len(numbers)} valid")


@app.command()
def evaluate(
    run: Annotated[int, typer.Argument(help="Run id, as printed by extract.")],
    errors: Annotated[bool, typer.Option(help="List every field the run got wrong.")] = False,
    data_dir: DataDir = Path("data"),
) -> None:
    """Score a run against the silver, gold and corrected labels."""
    conn = db.connect(data_dir / "radar.db")
    report = score_run(conn, run)
    typer.echo(to_markdown(report))
    if errors:
        typer.echo("")
        for example in report.examples:
            evidence = example["evidence"] or {}
            typer.echo(
                f"{example['notice']}  {example['field']}: {example['outcome']}, "
                f"expected {example['label']!r} ({example['label_source']}), "
                f"got {example['predicted']!r} p.{evidence.get('page')} "
                f"{(evidence.get('quote') or '')[:100]!r}"
            )


@app.command()
def sample(
    size: Annotated[int, typer.Option(help="How many notices.")] = 40,
    data_dir: DataDir = Path("data"),
) -> None:
    """Choose the evaluation set and write it to annotations/eval_set.txt."""
    conn = db.connect(data_dir / "radar.db")
    chosen = choose_sample(conn, size)
    EVAL_SET.parent.mkdir(exist_ok=True)
    EVAL_SET.write_text("\n".join(chosen) + "\n", encoding="utf-8")
    typer.echo(f"{len(chosen)} notices written to {EVAL_SET}")


@app.command()
def annotate(
    annotator: Annotated[str, typer.Option(help="Your name, stored with each answer.")],
    data_dir: DataDir = Path("data"),
) -> None:
    """Label the gold fields of every notice in the evaluation set, one at a time.

    Answers are saved after each notice, so you can stop with Ctrl+C and resume later.
    """
    conn = db.connect(data_dir / "radar.db")
    done = set(gold())
    todo = [n for n in EVAL_SET.read_text(encoding="utf-8").split() if n not in done]
    typer.echo(f"{len(done)} annotated, {len(todo)} to go. Rules: docs/annotation.md")
    for position, number in enumerate(todo, 1):
        row = conn.execute(
            "SELECT metadata, pdf_sha256 FROM notices WHERE control_number = ?", (number,)
        ).fetchone()
        tender = json.loads(row["metadata"])
        pages = pages_of(conn, number)
        typer.secho(f"\n[{position}/{len(todo)}] {number}", bold=True)
        typer.echo(f"{tender['orgaoEntidade']['razaoSocial']}")
        typer.echo(f"{tender['objetoCompra'][:300]}")
        typer.echo(f"PDF: {(data_dir / 'pdfs' / f'{row["pdf_sha256"]}.pdf').resolve()}")
        typer.echo(f"{len(pages)} pages. Open the PDF whenever the passages are not enough.")

        fields: dict[str, dict[str, object]] = {}
        for name in GOLD_FIELDS:
            typer.secho(f"\n{QUESTIONS[name]}", fg=typer.colors.CYAN)
            for hit in keyword_hits(pages, KEYWORDS[name]):
                typer.echo(f"  p.{hit.page}: {hit.snippet}")
            while True:
                valid, value = parse_answer(name, typer.prompt("  answer"))
                if valid:
                    break
                typer.echo("  Not one of the options; try again.")
            page = typer.prompt(
                "  page that says so (Enter to skip)", default="", show_default=False
            )
            fields[name] = {"value": value, "page": int(page) if page.strip().isdigit() else None}
        note = typer.prompt("Notes (Enter to skip)", default="", show_default=False)

        entry = {"control_number": number, "annotator": annotator, "fields": fields}
        if note:
            entry["note"] = note
        with (ANNOTATIONS / "gold.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")
        typer.secho("saved", fg=typer.colors.GREEN)


@app.command()
def review(
    run: Annotated[int, typer.Argument(help="Run whose disagreements with the API to review.")],
    reviewer: Annotated[str, typer.Option(help="Your name, stored with each decision.")],
    data_dir: DataDir = Path("data"),
) -> None:
    """Decide, reading the edital, who is right where the model and the API disagree.

    Every decision is saved to annotations/overrides.jsonl, including "the API was right",
    so the same disagreement is not asked again.
    """
    conn = db.connect(data_dir / "radar.db")
    report = score_run(conn, run)
    pending = [
        e for e in report.examples if e["label_source"] == "silver" and e["outcome"] != "missed"
    ]
    typer.echo(f"{len(pending)} disagreements with silver labels. Rules: docs/annotation.md")
    for position, example in enumerate(pending, 1):
        number, field_name = example["notice"], example["field"]
        evidence = example["evidence"] or {}
        pages = pages_of(conn, number)
        typer.secho(f"\n[{position}/{len(pending)}] {number}  {field_name}", bold=True)
        typer.echo(f"  API says:   {example['label']!r}")
        typer.echo(f"  model says: {example['predicted']!r}, p.{evidence.get('page')}")
        typer.echo(f"  quote: {evidence.get('quote')!r}")
        for hit in keyword_hits(pages, REVIEW_KEYWORDS.get(field_name, []), limit=4):
            typer.echo(f"  p.{hit.page}: {hit.snippet}")
        choice = typer.prompt("  [a]PI right, [m]odel right, [o]ther value, [s]kip").lower()
        if choice == "s":
            continue
        if choice == "a":
            value = example["label"]
        elif choice == "m":
            value = example["predicted"]
        else:
            value = parse_value(field_name, typer.prompt("  value (JSON, e.g. 5, true, null)"))
        reason = typer.prompt("  why (Enter to skip)", default="", show_default=False)
        entry = {
            "control_number": number,
            "field": field_name,
            "value": value,
            "reviewer": reviewer,
            "reason": reason or f"reviewed: {'API' if choice == 'a' else 'model'} right",
        }
        with (ANNOTATIONS / "overrides.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")
        typer.secho("  saved", fg=typer.colors.GREEN)


@app.command("export-dataset")
def export_dataset_command(data_dir: DataDir = Path("data")) -> None:
    """Write the evaluation set's notices to evals/dataset.jsonl.gz."""
    conn = db.connect(data_dir / "radar.db")
    count = export_dataset(conn, EVAL_SET.read_text(encoding="utf-8").split())
    typer.echo(f"{count} notices written to {DATASET}")


@app.command("export-run")
def export_run_command(
    run: Annotated[int, typer.Argument(help="Run id, as printed by extract.")],
    name: Annotated[str, typer.Argument(help="File name in evals/runs/, without .json.")],
    data_dir: DataDir = Path("data"),
) -> None:
    """Publish a run's answers to evals/runs/ so the report includes it."""
    conn = db.connect(data_dir / "radar.db")
    typer.echo(f"written {export_run(conn, run, name)}")


@app.command()
def report(
    check: Annotated[
        bool, typer.Option(help="Fail if evals/REPORT.md differs instead of writing it.")
    ] = False,
) -> None:
    """Score every published run from the files in evals/ and write evals/REPORT.md."""
    text = build_report()
    if check:
        current = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
        if current.replace("\r\n", "\n") != text:
            typer.echo(f"{REPORT} is out of date; run `radar report` and commit it.", err=True)
            raise typer.Exit(1)
        typer.echo(f"{REPORT} is up to date")
        return
    REPORT.write_text(text, encoding="utf-8", newline="\n")
    typer.echo(f"written {REPORT}")


@app.command("import-dataset")
def import_dataset_command(data_dir: DataDir = Path("data")) -> None:
    """Load evals/dataset.jsonl.gz into the database, so extract works without PDFs."""
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(data_dir / "radar.db")
    typer.echo(f"{import_dataset(conn)} notices loaded from {DATASET}")
