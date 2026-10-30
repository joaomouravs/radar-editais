# Radar de Editais

[![CI](https://github.com/joaomouravs/radar-editais/actions/workflows/ci.yml/badge.svg)](https://github.com/joaomouravs/radar-editais/actions/workflows/ci.yml)

Reads Brazilian public procurement notices (*editais*) with an LLM and turns each one
into a validated record in which every field cites the page and the passage it came
from. Then it measures, field by field, how often that record is right.

The extraction call is the easy part. The project is about the rest: where the expected
answers come from, what to do when the government's own API disagrees with the PDF,
how to tell a model that answered "not stated" correctly from one that missed the
answer, and how to fit 140,000-character documents into a free tier that allows 8,000
tokens a minute.

<!-- results -->

## What the data taught me

Every number here comes from the 152 *pregões eletrônicos* published in Rio de Janeiro
on 20 and 21 October 2026, and from the 40 of them in the evaluation set.

- **More than a quarter of the uploads are not PDFs.** 26 editais came inside a ZIP, 10
  as Word files and 6 in other formats (7z, RAR, old .doc). Inside a ZIP, the edital is the PDF whose name says *edital* or *pregão*
  and does not start like an annex (`Anexo`, `Apenso`, `Minuta`, `Termo de
  Referência`...), a rule tested against the real file names. 133 of 152 yield a PDF.
- **The API describes the tender, not the document.** PNCP's metadata looks like free
  labels, but in the first ten notices the API's estimated value was written in the
  edital only 3 times and its closing date only twice; the rest live in the procurement
  system. Scored naively, a model that correctly answered "not stated" would have been
  marked wrong. Silver labels for those two fields now count only when the edital
  spells them out.
- **The API is sometimes wrong.** The CEFET/RJ edital states R$ 2,812,241.48 on four
  pages; the API has R$ 2,812,281.48. Disagreements are reviewed by reading the edital
  (`radar review`) and recorded in `annotations/overrides.jsonl` with the reason.
- **Some editais are unfilled templates.** One says the session is on
  "Dia XX/XX/XXXX às XXh". The right answer is null, and the model gave it.
- **PNCP closes proposals one minute early.** The API records 09:59 for a session the
  edital announces at 10:00, so deadlines match within a minute.
- **Accuracy without a baseline is meaningless here.** 98% of the tenders are judged
  by lowest price, so a model that always answers "lowest price" scores 98% on that
  field. Every field in the report is shown next to the accuracy of always answering
  its most common label.
- **The model's characteristic mistake is reading template clauses as rules.** The
  federal template bars "the company, alone or in a consortium, that wrote the basic
  project" for conflict of interest. Models read it as "consortia are forbidden", even
  with the prompt warning about that exact clause.

## How it works

```
fetch ──> parse ──> extract ──> evaluate
 PNCP API   PDF to     LLM with    per-field accuracy vs. baseline,
 + edital   page text  strict      quotes checked against the page,
 (or ZIP)              JSON        tokens and latency
```

- **fetch** pages through the PNCP search API, picks the active edital among the
  tender's documents, finds it inside ZIP uploads, and stores the raw API metadata,
  which doubles as the silver labels.
- **parse** extracts text page by page with pypdfium2. Pages are the unit of evidence.
- **extract** asks for a fixed schema of ten fields, each `{value, page, quote}`,
  through strict structured output. The Pydantic models are the schema, and their
  field descriptions are the per-field instructions. An answer that does not validate
  (a cited page that does not exist, say) gets one retry with the error; a second
  failure is recorded, not hidden. Groq rejects off-schema output itself with HTTP 400,
  and that rejected JSON goes through the same retry.
- **Two context strategies.** *full* sends the whole edital. *retrieve* sends about
  12,000 characters of excerpts around each field's keywords, one match per field in
  turn, still tagged with their page, which is what fits Groq's free tier. The numbers
  in the report answer how much accuracy that costs.
- **evaluate** scores every field as correct, wrong, missed or invented, and checks
  that each quote is on the page it cites (ignoring case, spacing and line-break
  hyphenation, nothing else).

### Labels

| Source | Fields | How |
| --- | --- | --- |
| Silver | value, deadline, award criterion, dispute mode, SRP, small-business benefit | PNCP API, only where the edital states it |
| Override | any silver field | a disagreement reviewed by reading the edital |
| Gold | site visit, performance guarantee, term, consortium | annotated for all 40 notices, rules in [docs/annotation.md](docs/annotation.md) |

The gold labels were annotated by Claude (an LLM) reading each edital, blind to every
model's answers, and they say so in every entry. They are a second, independent reading
rather than human ground truth; a human-reviewed sample with its agreement rate is the
next step.

### Reproducibility

Model answers are cached on disk, so re-scoring never calls a model. The evaluation set
(40 notices: API metadata, items and page text) and every published run's answers live
in [`evals/`](evals); `radar report` scores them without a database or an API key, and
CI fails if [`evals/REPORT.md`](evals/REPORT.md) no longer follows from the code. The
manual *Live evaluation* workflow runs any model over the set in GitHub Actions.

## Running it

Requires [uv](https://docs.astral.sh/uv/). Put `GEMINI_API_KEY` and/or `GROQ_API_KEY`
in `.env` (see `.env.example`; both have free tiers).

```sh
uv sync
uv run radar fetch --from 2026-10-20 --to 2026-10-21 --uf RJ   # tenders and editais
uv run radar parse                                             # page text
uv run radar extract --model gemini:gemini-3.5-flash-lite --eval-set
uv run radar evaluate 1 --errors                               # scores, then every miss
uv run radar report                                            # evals/REPORT.md
```

Without downloading anything, `uv run radar import-dataset` loads the evaluation set
from `evals/` instead of the first two steps.

The design and its milestones are in [docs/design.md](docs/design.md).

## Limitations

- One state, two days of tenders, 40 evaluated notices: enough to compare models and
  strategies, not to quote a precise accuracy.
- Retrieval is keyword-based. It is cheap and explainable on templated documents; it
  would miss an edital that phrases a field in an unusual way.
- Word editais are counted and skipped: they have no pages to cite.

## License

MIT
