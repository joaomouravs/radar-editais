from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from radar import db
from radar.extract import extract, render_pages, save, start_run
from radar.llm import ChatClient, LlmError, Model
from radar.schema import NoticeExtraction, strict_json_schema

PAGES = ["EDITAL 49/2026", "Valor estimado: R$ 2.812.241,48. Vistoria facultativa."]

EMPTY = {"value": None, "evidence": None}


def answer(**fields) -> str:
    """A model answer with every field empty except the ones given."""
    return json.dumps({name: fields.get(name, EMPTY) for name in NoticeExtraction.model_fields})


VALID = answer(
    estimated_value={
        "value": 2812241.48,
        "evidence": {"page": 2, "quote": "Valor estimado: R$ 2.812.241,48"},
    },
    site_visit={"value": "optional", "evidence": {"page": 2, "quote": "Vistoria facultativa"}},
)


class FakeProvider:
    """Answers chat completions from a script and records what it was sent."""

    def __init__(self, *responses: httpx.Response | str):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        response = self.responses.pop(0)
        if isinstance(response, str):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": response}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
                },
            )
        return response


def client(provider: FakeProvider, cache_dir: Path, sleeps: list[float] | None = None):
    http = httpx.Client(transport=httpx.MockTransport(provider))
    return ChatClient(
        http,
        cache_dir,
        keys={"GEMINI_API_KEY": "test", "GROQ_API_KEY": "test"},
        sleep=(sleeps if sleeps is not None else []).append,
    )


MODEL = Model.parse("gemini:gemini-3.5-flash-lite")


def test_model_ids_round_trip():
    model = Model.parse("groq:openai/gpt-oss-120b@low")
    assert (model.provider, model.name, model.reasoning) == ("groq", "openai/gpt-oss-120b", "low")
    assert model.id == "groq:openai/gpt-oss-120b@low"
    with pytest.raises(ValueError):
        Model.parse("openai:gpt-5")


def test_strict_schema_has_no_references_and_requires_every_property():
    schema = strict_json_schema()
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and '"title"' not in text

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)
    # A description on the field survives inlining of its type.
    assert "Registro de Preços" in schema["properties"]["price_registration"]["description"]


def test_pages_are_tagged_with_their_numbers():
    assert render_pages(PAGES) == (
        "<page 1>\nEDITAL 49/2026\n</page 1>\n"
        "<page 2>\nValor estimado: R$ 2.812.241,48. Vistoria facultativa.\n</page 2>"
    )


def test_extract_sends_the_schema_and_validates_the_answer(tmp_path):
    provider = FakeProvider(VALID)

    result = extract(client(provider, tmp_path), MODEL, PAGES)

    assert result.output is not None
    assert result.output.estimated_value.value == 2812241.48
    assert result.output.site_visit.value == "optional"
    assert (result.attempts, result.tokens_in, result.cached) == (1, 1000, False)
    sent = provider.requests[0]
    assert sent["model"] == "gemini-3.5-flash-lite"
    assert sent["temperature"] == 0
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert "<page 2>" in sent["messages"][1]["content"]
    assert "reasoning_effort" not in sent


def test_an_invalid_answer_gets_one_retry_with_the_error(tmp_path):
    cites_missing_page = answer(
        estimated_value={"value": 1.0, "evidence": {"page": 9, "quote": "R$ 1,00"}}
    )
    provider = FakeProvider(cites_missing_page, VALID)

    result = extract(client(provider, tmp_path), MODEL, PAGES)

    assert result.output is not None and result.attempts == 2
    retry = provider.requests[1]["messages"]
    assert retry[2] == {"role": "assistant", "content": cites_missing_page}
    assert "page 9 does not exist" in retry[3]["content"]


def test_two_invalid_answers_are_a_recorded_failure(tmp_path):
    provider = FakeProvider("not json", '{"estimated_value": 3}')

    result = extract(client(provider, tmp_path), MODEL, PAGES)

    assert result.output is None
    assert result.attempts == 2
    assert result.error and result.error.startswith("invalid answer")
    assert result.raw == ["not json", '{"estimated_value": 3}']


def test_answers_are_cached_on_disk(tmp_path):
    extract(client(FakeProvider(VALID), tmp_path), MODEL, PAGES)
    # No responses left: a second call would fail if it reached the provider.
    again = extract(client(FakeProvider(), tmp_path), MODEL, PAGES)

    assert again.output is not None and again.cached
    # A different reasoning effort is a different request.
    other = Model.parse("gemini:gemini-3.5-flash-lite@low")
    assert extract(client(FakeProvider(VALID), tmp_path), other, PAGES).cached is False


def test_rate_limits_wait_for_retry_after(tmp_path):
    sleeps: list[float] = []
    provider = FakeProvider(
        httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(503), VALID
    )

    result = extract(client(provider, tmp_path, sleeps), MODEL, PAGES)

    assert result.output is not None
    assert sleeps == [7.0, 2.0]


def test_client_errors_are_not_retried(tmp_path):
    provider = FakeProvider(httpx.Response(400, text="context too long"))

    result = extract(client(provider, tmp_path), MODEL, PAGES)

    assert result.output is None and len(provider.requests) == 1
    assert result.error and "HTTP 400: context too long" in result.error


def test_a_missing_key_is_an_error(tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(FakeProvider(VALID)))
    with pytest.raises(LlmError, match="GROQ_API_KEY"):
        ChatClient(http, tmp_path, keys={}).complete(Model.parse("groq:qwen/qwen3.8-27b"), [], {})


def test_runs_and_predictions_are_stored(tmp_path):
    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO notices (control_number, cnpj, year, sequence, published_at, metadata, "
        "items, skip_reason) VALUES ('X-1/2026', '1', 2026, 1, '2026-10-20', '{}', '[]', 'test')"
    )
    run_id = start_run(conn, MODEL, "full")
    save(conn, run_id, "X-1/2026", extract(client(FakeProvider(VALID), tmp_path), MODEL, PAGES))

    row = conn.execute("SELECT * FROM predictions").fetchone()
    stored = NoticeExtraction.model_validate_json(row["output"])
    assert stored.estimated_value.value == 2812241.48
    assert (row["error"], row["attempts"], row["tokens_in"]) == (None, 1, 1000)
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert (run["model"], run["strategy"]) == ("gemini:gemini-3.5-flash-lite", "full")


def test_rate_limits_read_gemini_retry_delay_or_wait_most_of_a_minute(tmp_path):
    sleeps: list[float] = []
    gemini_429 = httpx.Response(
        429,
        json=[{"error": {"code": 429, "details": [{"retryDelay": "37s"}]}}],
    )
    provider = FakeProvider(gemini_429, httpx.Response(429), VALID)

    assert extract(client(provider, tmp_path, sleeps), MODEL, PAGES).output is not None
    assert sleeps == [37.0, 30.0]


def test_a_generation_groq_rejects_gets_the_usual_retry(tmp_path):
    rejected = answer(estimated_value={"value": 1.0, "evidence": {"page": None, "quote": "x"}})
    groq_400 = httpx.Response(
        400,
        json={
            "error": {
                "message": "Generated JSON does not match the expected schema.",
                "code": "json_validate_failed",
                "failed_generation": rejected,
            }
        },
    )
    provider = FakeProvider(groq_400, VALID)
    groq = Model.parse("groq:openai/gpt-oss-120b@low")

    result = extract(client(provider, tmp_path), groq, PAGES)

    assert result.output is not None and result.attempts == 2
    assert result.raw[0] == rejected
    assert "page" in provider.requests[1]["messages"][3]["content"]
