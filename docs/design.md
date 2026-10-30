# Radar de Editais: design

Status: implemented (M0 to M4). Deviations from the first draft are noted where they
happened.

## Problem

Brazilian public bodies publish their procurement notices (editais) on PNCP, the
national procurement portal. The portal's API gives the headline metadata of each
tender (object, estimated value, deadline, modality), but the information a supplier
needs to decide whether to bid lives inside the notice itself, a PDF that is often over
100 pages long: whether a site visit is mandatory, how big the performance guarantee is,
whether the tender is reserved for small businesses, how long the contract runs.

Radar reads those PDFs with an LLM and turns each notice into a validated, structured
record where every field points to the page and the passage it came from. The point of
the project is not the extraction call. It is knowing, with numbers, how often the
extraction is right, what it costs, and what changes when the model or the prompt
changes.

## Goals

1. Fetch notices and their PDFs from the public PNCP API.
2. Extract a fixed schema from each notice, validated by Pydantic, with evidence
   (page number and verbatim quote) for every non-empty field.
3. An evaluation harness that scores every run per field, checks that the evidence
   really is on the cited page, and reports cost and latency.
4. A comparison of models and context strategies, published in the README.
5. Questions about a notice answered with page citations (later milestone).

## Non-goals

- Legal advice. Radar summarises and points to the source; it does not interpret.
- Scanned PDFs. The first sample had a text layer on every page; OCR is deferred until
  the data shows it matters (measured, not assumed).
- Paid infrastructure. Everything runs on a laptop and free API tiers.

## Data source

PNCP has two public APIs with no authentication:

- `GET /api/consulta/v1/contratacoes/publicacao`: tenders published in a date range,
  filtered by modality and state. Returns the metadata used as labels (below).
- `GET /api/pncp/v1/orgaos/{cnpj}/compras/{year}/{seq}/arquivos`: the documents of a
  tender (edital, termo de referência, errata), each downloadable as a PDF.
- `GET /api/pncp/v1/orgaos/{cnpj}/compras/{year}/{seq}/itens`: the items, with the award
  criterion and the small-business benefit per item.

Scope for the dataset: electronic reverse auctions (pregão eletrônico, modality 6),
which are the most common kind, starting with the state of Rio de Janeiro.

### What the first sample looks like

All 152 pregões published in Rio de Janeiro on 20 and 21 October 2026:

| | Tenders |
| --- | --- |
| Edital uploaded as a PDF | 107 |
| Edital found inside a ZIP | 26 |
| Edital in Word (.docx, or .doc inside a ZIP) | 10 |
| Other formats (7z, RAR, .doc) | 6 |
| ZIP without an edital (terms of reference only, RAR inside) | 2 |
| No active edital | 1 |

So 133 of 152 (87%) yield a PDF. Inside a ZIP, the edital is the PDF whose name says
"edital" or "pregão" and does not start like an annex ("Anexo", "Apenso", "Minuta",
"Termo de Referência"...); when the ZIP holds a single PDF that is not an annex, that is
the edital. Word files are recorded with their own skip reason, because they have no
pages; supporting them is a decision for when the evaluation shows the gap matters.

The editais are long: median 52 pages and about 140,000 characters, 90th percentile
280,000 characters, longest 351 pages. Only one in 133 looks scanned (most pages
without text), which confirms deferring OCR.

## Extraction schema

| Field | Type | Label source |
| --- | --- | --- |
| `estimated_value` | decimal or null (null when the budget is confidential) | silver: API `valorTotalEstimado` |
| `proposal_deadline` | datetime | silver: API `dataEncerramentoProposta` |
| `award_criterion` | enum (lowest price, highest discount, ...) | silver: API items |
| `dispute_mode` | enum (open, closed, open-closed, closed-open) | silver: API `modoDisputaNome` |
| `price_registration` | bool (SRP) | silver: API `srp` |
| `small_business` | enum (exclusive, reserved quota, none) | silver: API items `tipoBeneficio` |
| `site_visit` | enum (mandatory, optional, not mentioned) | gold: hand annotation |
| `performance_guarantee_pct` | decimal or null | gold |
| `contract_term_months` | int or null | gold |
| `consortium_allowed` | bool or null | gold |

Every field is returned as `{value, page, quote}`. A field the notice does not mention
is `null` with no evidence, and saying "not mentioned" correctly counts as a hit.

**Silver labels** come free from the API metadata, so they scale to hundreds of notices
with no annotation. *(Changed after the first scores: the value and the closing date
count only when the edital states them, because the API had them written in the PDF in
only 3 and 2 of the first ten notices.)* They are not perfect (the PDF and the API can disagree, for example
after an errata), and disagreements are reviewed by hand rather than assumed to be model
errors. **Gold labels** cover the fields the API does not have, for the 40 notices of
the evaluation set, and the annotation file is versioned in the repository. *(They were
annotated by Claude, an LLM, reading each edital blind to the models' answers, under
the rules in `annotation.md`; a human-reviewed sample is planned to measure agreement.)*

## Pipeline

```
fetch ──> parse ──> extract ──> evaluate
 API       PDF to     LLM with     per-field score,
 + PDF     page text  structured   evidence check,
                      output       cost and latency
```

1. **fetch** stores the raw API JSON in SQLite and the edital PDF on disk, keyed by the
   PNCP control number, with its SHA-256.
2. **parse** extracts text page by page with pypdfium2. Pages are the unit of evidence.
3. **extract** builds the prompt, calls the model and validates the answer against the
   Pydantic schema. An invalid answer gets one retry with the validation error in the
   prompt; a second failure is recorded as a failure, not hidden.
4. **evaluate** compares predictions with labels and writes a report (Markdown and JSON).

Each run is recorded with the model, the prompt version, the strategy, tokens in and
out, latency and the computed cost, so two runs can always be compared.

## The context problem

The first notice I downloaded (CEFET/RJ, pregão 49/2026) has 136 pages and about
110,000 tokens. That fits in a long-context model, but free tiers limit tokens per
minute, and sending the whole document for every field is slow and expensive at any
price. So the project compares two strategies:

- **full**: the whole document in one request (only for models with a large context).
- **retrieve**: send excerpts around each field's keywords, one match per field in
  turn, tagged with their page, about 12,000 characters in all (measured: that keeps
  even a validation retry under Groq's 8,000 tokens a minute). Keywords work because
  the notices follow the federal AGU template; embeddings come only if the
  measurements show keywords missing passages.

The question the evaluation answers: how much accuracy does retrieval cost, and how
much money and time does it save?

## Evaluation

- **Field accuracy**: exact match after normalisation (values in cents, dates in UTC,
  enums mapped), per field and overall.
- **Not-mentioned handling**: precision and recall for `null`, because inventing a
  guarantee that does not exist is worse than missing one.
- **Grounding rate**: share of non-null fields whose quote is found on the cited page
  (whitespace and hyphenation normalised). A low grounding rate means the model is
  inventing evidence, even when the value happens to be right.
- **Cost and latency**: per notice, p50 and p95.

Model responses are cached on disk by a hash of (model, prompt, input). Re-scoring a run
costs nothing, and the cached responses for a fixed evaluation subset are committed, so
CI recomputes the metrics on every push without an API key. Live runs against the
models are a manual GitHub Actions workflow, the same pattern as the Carteiro load test.

## Models

The client speaks the OpenAI-compatible chat API, which Google Gemini and Groq both
offer, so the provider is configuration. The comparison starts with one large-context
model (Gemini Flash) and one small, fast open model on Groq. A `fake` provider returns
canned answers for tests.

## Stack

Python 3.12 (managed by uv), Pydantic v2, httpx, pypdfium2 (Apache/BSD; PyMuPDF was
left out because its AGPL licence would bind the whole project), SQLite from the
standard library, Typer for the CLI, pytest, ruff and pyright.

## Milestones

- **M0**: project skeleton, CI, `fetch` and `parse` with tests on recorded API fixtures.
- **M1**: schema, prompt, `extract` with the full strategy, response cache.
- **M2**: evaluation harness, silver labels, 40 gold annotations, first report.
- **M3**: retrieve strategy, second model, comparison table in the README.
- **M4**: questions with page citations (`radar ask`, BM25 over chunks, every
  citation checked), README, write-up.
