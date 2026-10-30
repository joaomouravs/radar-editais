"""Score a run against the labels: per-field accuracy next to the majority-class baseline,
how "not stated" is handled, whether the quotes are really on the cited pages, and what
the run cost in tokens and time."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import quantiles
from typing import Any

from radar.labels import ANNOTATIONS, Label, expected
from radar.schema import FIELDS, NoticeExtraction

OUTCOMES = ["correct", "wrong", "missed", "invented"]


@dataclass
class FieldScore:
    outcomes: Counter[str] = field(default_factory=Counter)
    # Gold labels say "not stated" (None); silver labels never do.
    sources: Counter[str] = field(default_factory=Counter)
    label_values: Counter[str] = field(default_factory=Counter)

    @property
    def labelled(self) -> int:
        return sum(self.outcomes.values())

    @property
    def accuracy(self) -> float | None:
        return self.outcomes["correct"] / self.labelled if self.labelled else None

    @property
    def baseline(self) -> float | None:
        """Accuracy of always answering the most common label."""
        if not self.labelled:
            return None
        return self.label_values.most_common(1)[0][1] / self.labelled


@dataclass
class Report:
    run_id: int
    model: str
    notices: int = 0
    failures: int = 0
    fields: dict[str, FieldScore] = field(default_factory=lambda: {f: FieldScore() for f in FIELDS})
    grounding: Counter[str] = field(default_factory=Counter)
    tokens_in: list[int] = field(default_factory=list)
    tokens_out: list[int] = field(default_factory=list)
    latency_ms: list[int] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)


def same(field_name: str, predicted: Any, label: Any) -> bool:
    if predicted is None or label is None:
        return predicted is label
    if field_name == "estimated_value":
        return abs(float(predicted) - float(label)) < 0.005
    if field_name == "proposal_deadline":
        # PNCP records proposals as closing one minute before the session the edital
        # announces (09:59 for a 10:00 session), so one minute apart is the same deadline.
        try:
            gap = datetime.fromisoformat(str(predicted)[:16]) - datetime.fromisoformat(
                str(label)[:16]
            )
        except ValueError:
            return False
        return abs(gap) <= timedelta(minutes=1)
    if field_name == "performance_guarantee_pct":
        return abs(float(predicted) - float(label)) < 0.01
    return predicted == label


def outcome(field_name: str, predicted: Any, label: Label) -> str:
    if same(field_name, predicted, label.value):
        return "correct"
    if predicted is None:
        return "missed"
    if label.value is None:
        return "invented"
    return "wrong"


def _squash(text: str) -> str:
    """Compare text ignoring case, spacing and line-break hyphenation, which PDF text
    extraction and models both change freely; every other character must match."""
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    text = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"\s+", "", text).casefold()


def ground(quote: str, page: int, pages: list[str]) -> str:
    """'on_page' when the quote is on the cited page, 'other_page' when it is elsewhere
    in the document, 'not_found' when the model made it up or rewrote it."""
    needle = _squash(quote)
    if not needle:
        return "not_found"
    if 1 <= page <= len(pages) and needle in _squash(pages[page - 1]):
        return "on_page"
    if any(needle in _squash(text) for text in pages):
        return "other_page"
    return "not_found"


def evaluate(conn: sqlite3.Connection, run_id: int, annotations: Path = ANNOTATIONS) -> Report:
    run = conn.execute("SELECT model FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"run {run_id} does not exist")
    report = Report(run_id, run["model"])
    rows = conn.execute(
        """
        SELECT p.*, n.metadata, n.items FROM predictions p
        JOIN notices n USING (control_number)
        WHERE p.run_id = ? ORDER BY p.control_number
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        report.notices += 1
        if row["output"] is None:
            report.failures += 1
            continue
        report.tokens_in.append(row["tokens_in"])
        report.tokens_out.append(row["tokens_out"])
        report.latency_ms.append(row["latency_ms"])
        output = NoticeExtraction.model_validate_json(row["output"])
        pages = [
            r["text"]
            for r in conn.execute(
                "SELECT text FROM pages WHERE control_number = ? ORDER BY page",
                (row["control_number"],),
            )
        ]
        labels = expected(
            row["control_number"],
            json.loads(row["metadata"]),
            json.loads(row["items"]),
            pages,
            annotations,
        )
        for name in FIELDS:
            answer = getattr(output, name)
            if answer.evidence is not None and answer.value is not None:
                report.grounding[ground(answer.evidence.quote, answer.evidence.page, pages)] += 1
            if name not in labels:
                continue
            label = labels[name]
            result = outcome(name, answer.value, label)
            score = report.fields[name]
            score.outcomes[result] += 1
            score.sources[label.source] += 1
            score.label_values[json.dumps(label.value)] += 1
            if result != "correct":
                report.examples.append(
                    {
                        "notice": row["control_number"],
                        "field": name,
                        "outcome": result,
                        "label": label.value,
                        "label_source": label.source,
                        "predicted": answer.value,
                        "evidence": answer.evidence.model_dump() if answer.evidence else None,
                    }
                )
    return report


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _p(values: list[int], q: int) -> int:
    if len(values) < 2:
        return values[0] if values else 0
    return round(quantiles(values, n=100, method="inclusive")[q - 1])


def to_markdown(report: Report) -> str:
    lines = [
        f"## Run {report.run_id}: `{report.model}`",
        "",
        f"{report.notices} notices, {report.failures} without a valid answer.",
        "",
        "| Field | Labelled | Accuracy | Baseline | Wrong | Missed | Invented | Labels |",
        "| --- | --: | --: | --: | --: | --: | --: | --- |",
    ]
    correct = labelled = 0
    for name, score in report.fields.items():
        if not score.labelled:
            continue
        correct += score.outcomes["correct"]
        labelled += score.labelled
        sources = ", ".join(f"{n} {s}" for s, n in score.sources.most_common())
        lines.append(
            f"| `{name}` | {score.labelled} | {_pct(score.accuracy)} | {_pct(score.baseline)} "
            f"| {score.outcomes['wrong']} | {score.outcomes['missed']} "
            f"| {score.outcomes['invented']} | {sources} |"
        )
    total = sum(report.grounding.values())
    lines += [
        "",
        f"Overall: {_pct(correct / labelled if labelled else None)} of {labelled} labelled fields.",
        "",
        f"Evidence: {total} quotes, {_pct(report.grounding['on_page'] / total if total else None)}"
        f" on the cited page, {report.grounding['other_page']} on another page, "
        f"{report.grounding['not_found']} not in the document.",
        "",
        f"Tokens per valid answer: p50 {_p(report.tokens_in, 50)} in, "
        f"{_p(report.tokens_out, 50)} out. "
        f"Latency: p50 {_p(report.latency_ms, 50) / 1000:.1f}s, "
        f"p95 {_p(report.latency_ms, 95) / 1000:.1f}s.",
    ]
    return "\n".join(lines)
