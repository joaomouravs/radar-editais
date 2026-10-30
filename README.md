# Radar de Editais

Structured, evaluated extraction from Brazilian public procurement notices (editais).

Work in progress. See the [design document](docs/design.md) for the goals, the
extraction schema and how the evaluation works.

## Running

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run radar fetch --from 2026-10-20 --to 2026-10-21 --uf RJ --limit 20
uv run radar parse
uv run radar stats
```

Data goes to `data/` (the SQLite database and the PDFs), which is not versioned.
