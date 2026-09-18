# KPI Crawler

A foundation for acquiring documents (currently PDFs) from the web or the local
filesystem, extracting structured evidence from them, and persisting everything
in PostgreSQL with full provenance back to the original source.

This is a deliberately small, current-phase implementation. See
[`AGENTS.md`](AGENTS.md) for the engineering rules this project is built under
(no speculative abstractions, no relevance/scoring/intelligence features yet)
and [`progress.md`](progress.md) for the detailed, running log of what has been
built, verified, and left unresolved.

## What it does

1. **Discover** — given a URL (or a local path), optionally crawl outward
   following HTML links, sitemap entries, and `robots.txt`-declared sitemaps,
   breadth-first, bounded by depth and artifact count.
2. **Acquire** — fetch each discovered resource over HTTP(S), `file://`, or a
   plain local path, with bounded timeouts/retries and a size limit enforced
   before unbounded buffering.
3. **Store** — persist the raw bytes content-addressed by SHA-256, so the same
   content is never stored or processed twice, no matter how many URLs point
   to it.
4. **Extract** — parse supported content (currently `application/pdf`, via
   Docling) into evidence: `text`, `table`, and `visual` records, each keeping
   its claim/value, full supporting context, page number, and bounding box.
5. **Persist** — write runs, acquisition events (with parent/child lineage),
   artifacts, and evidence to PostgreSQL as the system of record.
6. **Export** — produce a human-readable, self-contained snapshot of a
   completed run (Markdown README, JSON manifest, original files, JSONL
   evidence) for inspection outside the database.

Every acquisition and extraction outcome — success, rejection, or failure —
is recorded as a queryable event, so `discovered=/acquired=/rejected=` is
always a real accounting of what happened, not just a log line.

## Requirements

- Python 3.12 (see `.python-version`)
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- PostgreSQL (a local dev instance is provided via `docker-compose.yml`)

## Setup

```bash
# Install dependencies (including dev/test dependencies)
uv sync --dev

# Start a local PostgreSQL instance
docker compose up -d

# Configure environment
cp .env.example .env
# edit .env if needed — DATABASE_URL is required, everything else has a default

# Apply database migrations
uv run kpi-crawler migrate
```

## Configuration

All configuration is environment-based (`kpi_crawler/config.py`). See
[`.env.example`](.env.example) for the full list with defaults:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | yes | — | PostgreSQL connection string |
| `LOG_LEVEL` | no | `INFO` | standard Python log level |
| `ARTIFACT_STORAGE_DIR` | no | `.data/artifacts` | raw content-addressed storage |
| `EXPORT_DIR` | no | `exports` | destination for `export` output |
| `ACQUISITION_TIMEOUT_SECONDS` | no | `10` | per-request timeout |
| `ACQUISITION_RETRIES` | no | `2` | retryable transport/HTTP failures |
| `MAX_ARTIFACT_BYTES` | no | `50000000` | size limit, enforced before buffering |
| `ALLOWED_CONTENT_TYPES` | no | `application/pdf` | comma-separated allow-list |
| `CRAWL_MAX_DEPTH` | no | `2` | link-following depth bound |
| `CRAWL_MAX_ARTIFACTS` | no | `50` | per-run acquisition cap |

## CLI usage

```bash
# Validate configuration and start the application boundary
uv run kpi-crawler start

# Apply pending SQL migrations
uv run kpi-crawler migrate

# Process a single source end-to-end (acquire, store, extract, persist)
uv run kpi-crawler process <source-url>

# Breadth-first crawl from a root URL (links, sitemaps, robots.txt)
uv run kpi-crawler crawl <source-url>

# Export a completed run to a human-readable directory
uv run kpi-crawler export <run-id>
```

Expected operational failures (acquisition, extraction, storage, unsupported
content, operational limits) are logged with a clear category and a
`discovered=/acquired=/rejected=` summary — never an unhandled traceback.

## Project layout

```
kpi_crawler/
  config.py         Environment-based Settings
  db.py              PostgreSQL connection boundary
  migrations.py      SQL migration discovery/runner
  migrations/         Versioned SQL migrations
  acquisition.py     HTTP(S)/file/local fetch, storage, event recording
  discovery.py       Pure link/sitemap/robots.txt parsing (no I/O)
  crawl.py            Breadth-first crawl orchestration
  extraction.py       Artifact -> Extractor -> ExtractionResult contract
  evidence_serialization.py   Canonical per-evidence-type text serializers
  pipeline.py         Shared acquire/store/extract-if-supported flow
  export.py           Read-only, human-readable run export
  models.py           Storage dataclasses
  errors.py           Typed, expected-failure exception hierarchy
  storage.py          AcquisitionRepository (PostgreSQL persistence)
  logging.py          Shared logging configuration
  cli.py              Command-line entry point
tests/                Unit and PostgreSQL integration tests
```

## Testing

```bash
# Unit tests only (PostgreSQL integration tests are skipped automatically
# when DATABASE_URL/PostgreSQL isn't available)
uv run pytest -q

# Full suite, including PostgreSQL integration tests
docker compose up -d
uv run kpi-crawler migrate
uv run pytest -q
```

## Data model

- **`acquisition_runs`** — one row per `process`/`crawl` invocation.
- **`acquisition_events`** — one row per discovery/acquisition/extraction
  attempt, with `parent_event_id` lineage (e.g. a discovered link points back
  to the event of the page it was found on).
- **`artifacts`** — content-addressed (SHA-256) raw content, immutable at the
  database layer (update/delete-rejecting triggers).
- **`extractions`** — one row per extraction attempt against an artifact; at
  most one `succeeded` row per artifact is enforced by a unique constraint.
- **`evidence`** — `text`/`table`/`visual` records with claim, value,
  supporting context, structure (JSONB), page number, bounding box, and a
  deterministic `canonical_text`/`canonical_serializer_version`.

## Status

The current phase is human-readable run export. Crawler/relevance/scoring
intelligence features are explicitly out of scope for now — see
[`AGENTS.md`](AGENTS.md) and the "Current phase" section of
[`progress.md`](progress.md).
