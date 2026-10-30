"""Ask a model for the extraction schema and keep only answers that validate."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from pydantic import ValidationError

from radar.llm import ChatClient, LlmError, Model
from radar.schema import PROMPT_VERSION, NoticeExtraction, strict_json_schema

SYSTEM_PROMPT = """\
You read Brazilian public procurement notices (editais de licitação) and fill in a JSON \
schema. The document is given page by page, each page wrapped in <page n>...</page n>.

Rules:
- Use only what the document says. Do not infer a value from common practice.
- For every non-null value, give the page it is on and a short verbatim quote copied \
exactly from that page. Do not fix typos or change spacing in the quote.
- When the document does not state a field, the value and the evidence are both null.
- When the edital and an annex disagree, the edital prevails."""

# One answer that fails validation gets a second chance with the error in the prompt.
MAX_ATTEMPTS = 2


@dataclass
class Result:
    output: NoticeExtraction | None
    error: str | None = None
    attempts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    cached: bool = False
    raw: list[str] = field(default_factory=list)


def render_pages(pages: list[str]) -> str:
    return "\n".join(f"<page {n}>\n{text.strip()}\n</page {n}>" for n, text in enumerate(pages, 1))


def extract(client: ChatClient, model: Model, pages: list[str]) -> Result:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_pages(pages)},
    ]
    result = Result(output=None)
    for _ in range(MAX_ATTEMPTS):
        result.attempts += 1
        try:
            completion = client.complete(model, messages, strict_json_schema())
        except LlmError as exc:
            result.error = str(exc)
            return result
        result.tokens_in += completion.tokens_in
        result.tokens_out += completion.tokens_out
        result.latency_ms += completion.latency_ms
        # Cached only if every answer in this result came from the cache.
        result.cached = completion.cached and (result.attempts == 1 or result.cached)
        result.raw.append(completion.content)
        try:
            result.output = NoticeExtraction.model_validate_json(
                completion.content, context={"pages": len(pages)}
            )
            result.error = None
            return result
        except ValidationError as exc:
            result.error = f"invalid answer: {exc.errors(include_url=False)}"
            messages += [
                {"role": "assistant", "content": completion.content},
                {
                    "role": "user",
                    "content": f"That answer does not validate:\n{exc}\n"
                    "Answer again with the corrected JSON only.",
                },
            ]
    return result


def start_run(conn: sqlite3.Connection, model: Model, strategy: str) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO runs (model, strategy, prompt_version) VALUES (?, ?, ?)",
            (model.id, strategy, PROMPT_VERSION),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def save(conn: sqlite3.Connection, run_id: int, control_number: str, result: Result) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO predictions (run_id, control_number, output, error, attempts,
                                     tokens_in, tokens_out, latency_ms, cached, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                control_number,
                result.output.model_dump_json() if result.output else None,
                result.error,
                result.attempts,
                result.tokens_in,
                result.tokens_out,
                result.latency_ms,
                result.cached,
                json.dumps(result.raw, ensure_ascii=False),
            ),
        )


def pages_of(conn: sqlite3.Connection, control_number: str) -> list[str]:
    rows = conn.execute(
        "SELECT text FROM pages WHERE control_number = ? ORDER BY page", (control_number,)
    )
    return [row["text"] for row in rows]
