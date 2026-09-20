# KPI Crawler (Program 1)

A bounded, provenance-first, source-agnostic acquisition and extraction pipeline that discovers and acquires web resources, PDFs, and local files, preserves raw artifacts and evidence with traceability, and exports structured evidence.

> **Current Status Summary:** Program 1 is a bounded, provenance-first acquisition and extraction pipeline that discovers and acquires static web resources, PDFs, and local sources. It features an adaptive acquisition engine with Playwright headless browser escalation, dynamic browser fingerprint injection (Crawlee / Browserforge), proxy pool management with quarantine and rotation, per-domain session lifecycle tracking, and adaptive concurrency throttling on HTTP 429 rate limits. It preserves raw artifacts and extracted evidence with strict provenance and exports human-readable handoff packages.

---

## Table of Contents

- [Purpose & Architecture](#purpose--architecture)
- [How It Works](#how-it-works)
  - [Pipeline Flow](#pipeline-flow)
  - [Failure as a First-Class Outcome](#failure-as-a-first-class-outcome)
  - [Data Model & Persistence](#data-model--persistence)
  - [Handoff & Export Package](#handoff--export-package)
- [What Program 1 Covers (Implemented)](#what-program-1-covers-implemented)
  - [1. Acquisition, Crawling & Discovery](#1-acquisition-crawling--discovery)
  - [2. Adaptive Engine: Browser Escalation, Fingerprinting, Proxies & Sessions](#2-adaptive-engine-browser-escalation-fingerprinting-proxies--sessions)
  - [3. Extraction & Document Processing](#3-extraction--document-processing)
  - [4. Resource Safety & Boundedness](#4-resource-safety--boundedness)
  - [5. Provenance Chain](#5-provenance-chain)
- [What Program 1 Does NOT Cover (Explicit Boundaries)](#what-program-1-does-not-cover-explicit-boundaries)
  - [1. `robots.txt` Path Rules (`Disallow`/`Allow`)](#1-robotstxt-path-rules-disallowallow)
  - [2. Anti-Bot Challenge & CAPTCHA Bypass](#2-anti-bot-challenge--captcha-bypass)
  - [3. Interactive Web Navigation](#3-interactive-web-navigation)
- [Setup & Requirements](#setup--requirements)
- [Configuration](#configuration)
- [CLI Reference & How to Run](#cli-reference--how-to-run)
  - [1. Main Pipeline (`kpi-crawler`)](#1-main-pipeline-kpi-crawler)
  - [2. Adaptive Acquisition Engine (`kpi-crawler-acquire`)](#2-adaptive-acquisition-engine-kpi-crawler-acquire)
  - [3. Semantic Retrieval Baseline (`kpi-crawler-retrieval`)](#3-semantic-retrieval-baseline-kpi-crawler-retrieval)
- [Testing](#testing)
- [Project Layout](#project-layout)

---

## Purpose & Architecture

**Program 1 is the generic Acquisition + Extraction system.**

Its core mandate:
> Given a source, discover reachable resources, acquire the raw artifacts, extract structured evidence, preserve end-to-end provenance, and hand that evidence to downstream systems.

```text
SOURCE (URL or local path)
  ↓
DISCOVERY (HTML links, sitemaps, robots.txt directives)
  ↓
ACQUISITION (HTTP/HTTPS, file://, local paths)
  ↓
RAW ARTIFACT (SHA-256 content-addressed storage)
  ↓
EXTRACTION (Docling PDF extraction: text, tables, visual context)
  ↓
EVIDENCE + PROVENANCE (Claim, value, structure, bounding boxes, page)
  ↓
EXPORT / DATABASE (PostgreSQL runtime store + portable file package)
  ↓
Downstream Systems (Program 2 / Retrieval Baseline)
```

---

## How It Works

### Pipeline Flow

1. **Discovery** — Evaluates sources. Given an initial URL (or local file path), link parsing extracts outbound hyperlinks, standard `rel="next"` pagination links, XML sitemaps, and sitemap URLs declared inside `/robots.txt`. Standards-based resource references (`<link rel="stylesheet">`, `rel="icon"`, `rel="preload"`) are recognized and excluded from expanding the crawl frontier. Traversal proceeds breadth-first up to configurable depth and artifact limits.
2. **Acquisition** — Fetches discovered resources over HTTP(S) or local paths. Applies bounded timeouts, retries, and checks the `Content-Length` header prior to downloading to avoid buffering oversized artifacts. For dynamic client-rendered sites, the adaptive engine escalates to headless Playwright Chromium.
3. **Storage** — Stores acquired bytes in content-addressed storage indexed by SHA-256. If multiple URLs point to byte-for-byte identical content, it is stored only once and extraction is deduplicated.
4. **Extraction** — Dispatches supported content types (currently `application/pdf` via Docling) to generate structured evidence items categorized as `text`, `table`, or `visual`. Captures headings, section context, page numbers, and bounding-box coordinates.
5. **Persistence** — Writes runs, acquisition events (with parent-to-child lineage), artifacts, extractions, and evidence records to PostgreSQL.
6. **Export** — Produces a standalone, human-readable handoff package containing run manifests, metadata, original artifacts, JSONL evidence, and a zero-dependency static HTML report (`report.html`).

### Failure as a First-Class Outcome

Program 1 treats failure as a durable, queryable state rather than crashing or silently dropping targets. When a resource returns a 404, 403, or invalid content, a structured acquisition failure event is written to the database.

```text
                    Source Target
                          │
                          ▼
                     Acquisition
                          │
               ┌──────────┴──────────┐
               │                     │
            Success               Failure
               │                     │
               ▼                     ▼
           Artifact            Durable Event
               │               (status/error logged)
               ▼
           Extraction
               │
          ┌────┴─────┐
          │          │
       Success   Unsupported/
          │      Extraction Error
          ▼          │
       Evidence      ▼
               Event Recorded
```

### Data Model & Persistence

PostgreSQL serves as the primary system of record:

- **`acquisition_runs`**: One record per `process` or `crawl` run.
- **`acquisition_events`**: Detailed log for every discovery, acquisition, or extraction attempt, preserving hierarchical lineage via `parent_event_id`.
- **`artifacts`**: Content-addressed (SHA-256) storage metadata for raw resources. Database triggers enforce immutability (rejecting updates and deletions).
- **`extractions`**: Records each extraction run against an artifact, enforcing a unique constraint on successful extractions per artifact.
- **`evidence`**: Granular evidence entries (`text`, `table`, `visual`) containing claim, value, supporting context, structural metadata (JSONB), page number, bounding box, and deterministic `canonical_text` serialization.

### Handoff & Export Package

Running `kpi-crawler export <run-id>` creates an inspectable, portable directory on disk:

```text
exports/run-<id>/
├── README.md              # Human-readable run overview and statistics
├── manifest.json          # Machine-readable metadata and artifact index
├── report.html            # Zero-dependency interactive browser report
└── sources/
    └── <artifact_id>/
        ├── source.json    # Origin URL, acquisition timestamp, content type
        ├── original.pdf   # Exact raw bytes stored
        └── evidence.jsonl # Extracted evidence with bounding boxes & context
```

The adaptive acquisition engine (`kpi-crawler-acquire`) writes isolated run outputs under `.data/runs/<site_id>/<run_id>/` with raw files sorted by MIME type (`raw/html`, `raw/pdf`, `raw/json`), execution ledgers (`evidence/attempts.jsonl`, `evidence/adaptive_decisions.jsonl`), and downstream handoff files (`program2/artifacts.json`, `metadata.json`, `provenance.json`).

---

## What Program 1 Covers (Implemented)

### 1. Acquisition, Crawling & Discovery

| Capability | Status | Current Behavior |
|---|---|---|
| HTTP/HTTPS acquisition | ✅ Implemented | Fetches resources over HTTP and HTTPS |
| Local files | ✅ Implemented | Supports `file://` URIs and plain local file paths |
| URL discovery | ✅ Implemented | Discovers standard HTML links (`<a href="...">`) |
| Sitemap discovery | ✅ Implemented | Parses `sitemap.xml` and sitemap index files |
| Robots sitemap discovery | ✅ Implemented | Reads `Sitemap:` directives from `/robots.txt` (RFC 9309) |
| Non-page link filtering | ✅ Implemented | Skips `<link rel="stylesheet">`, `rel="icon"`, `rel="preload"`, etc. from crawl queue |
| Pagination | ✅ Implemented | Follows standards-based `rel="next"` links |
| URL deduplication | ✅ Implemented | Per-run visited set, fragment-insensitive |
| Content deduplication | ✅ Implemented | Content-addressed SHA-256 artifact hashing |
| Redirect handling | ✅ Implemented | Resolves and records HTTP redirects |
| Same-host restriction | ✅ Implemented | Restricts crawling to originating host by default |
| Crawl depth limit | ✅ Implemented | Configurable maximum crawl depth |
| Artifact count cap | ✅ Implemented | Configurable maximum resources acquired |
| Retry handling | ✅ Implemented | Configurable retry count for transient transport failures |
| Request timeout | ✅ Implemented | Configurable per-request network timeouts |
| Maximum artifact size | ✅ Implemented | Enforces byte limit before and during body buffering |
| Content-type validation | ✅ Implemented | Validates against configured allowed MIME types |
| Failure persistence | ✅ Implemented | Durable failure recording in `acquisition_events` |
| Acquisition lineage | ✅ Implemented | `parent_event_id` links child discoveries to source pages |

### 2. Adaptive Engine: Browser Escalation, Fingerprinting, Proxies & Sessions

The adaptive acquisition engine (`kpi-crawler-acquire`) provides sophisticated acquisition handling:

| Capability | Status | Implementation Details |
|---|---|---|
| Headless browser escalation | ✅ Implemented | Escalates from HTTP to headless Chromium via Playwright when dynamic content is detected |
| Dynamic content detection | ✅ Implemented | Detects SPA-shell pages via script-to-content heuristics (`dynamic_content_likely`) |
| Browser fingerprint injection | ✅ Implemented | Uses Crawlee's `DefaultFingerprintGenerator` + `browserforge` to inject realistic fingerprints |
| Fingerprint consistency | ✅ Implemented | Fingerprints are assigned per session and reused across all pages in that context |
| Safe fingerprint reporting | ✅ Implemented | Verbose CLI logs display safe descriptor summaries (`platform:digest`) |
| Proxy pool management | ✅ Implemented | `ProxyPool` supports configurable upstream proxies with round-robin rotation |
| Proxy health & quarantine | ✅ Implemented | Automatically tracks failures, quarantines unhealthy proxies, and restores on recovery |
| Credential redaction | ✅ Implemented | Strips embedded user credentials (`user:pass@`) from logs and CLI outputs |
| Session lifecycle tracking | ✅ Implemented | Per-domain session pool (`pool_size=4`) reuses identities across URLs; retires on repeated failures |
| Non-penalizing 404s | ✅ Implemented | 404 (`NOT_FOUND`) responses on pages/robots.txt/sitemap do not degrade proxy or session health |
| Adaptive rate limiting | ✅ Implemented | Detects HTTP 429 rate limits and dynamically reduces concurrency (`concurrency_reduced`) |
| Adaptive backoff & recovery | ✅ Implemented | Applies backoff delay and gradually restores concurrency on healthy streaks |
| Append-only evidence ledger | ✅ Implemented | Persists all attempts and adaptive decisions to `evidence/attempts.jsonl` and `adaptive_decisions.jsonl` |

### 3. Extraction & Document Processing

| Capability | Status | Details |
|---|---|---|
| PDF extraction | ✅ Implemented | Powered by Docling |
| Text extraction | ✅ Implemented | Paragraphs and inline text with contextual headings |
| Heading & section hierarchy | ✅ Implemented | Relative-height heuristics capture document hierarchy |
| Page number tracking | ✅ Implemented | 1-indexed document page numbering |
| Bounding-box location | ✅ Implemented | Preserves coordinate geometry on original pages |
| Table extraction | ✅ Implemented | Structured tables with paired row/column layouts |
| Visual context | ✅ Implemented | Locates embedded images and visual elements |
| Canonical serialization | ✅ Implemented | Versioned deterministic text representation per item |
| Evidence types | ✅ Implemented | `text`, `table`, and `visual` categories |

### 4. Resource Safety & Boundedness

| Protection | Status | Mechanism |
|---|---|---|
| Request timeout | ✅ Implemented | Halts hanging connections |
| Retry bounds | ✅ Implemented | Prevents infinite retry loops |
| `Content-Length` pre-check | ✅ Implemented | Rejects oversized payloads before downloading body |
| Bounded streaming | ✅ Implemented | Aborts stream if bytes exceed limit mid-download |
| Crawl depth bounds | ✅ Implemented | BFS traversal halts at `CRAWL_MAX_DEPTH` |
| Artifact count caps | ✅ Implemented | Stops processing when `CRAWL_MAX_ARTIFACTS` is met |
| Immutability triggers | ✅ Implemented | PostgreSQL triggers reject modifying stored artifacts |
| Resource cleanup | ✅ Implemented | Ensures network connections and files are closed |

### 5. Provenance Chain

Program 1 maintains an unbroken chain of custody from origin to evidence:

```text
Source URL
  ↓
Acquisition Event (timestamp, headers, lineage)
  ↓
Artifact (immutable SHA-256 byte payload)
  ↓
Extraction Event (extractor version, configuration)
  ↓
Evidence Row (claim, value, bounding box, page number)
```

---

## What Program 1 Does NOT Cover (Explicit Boundaries)

To keep the architecture explicit and prevent scope creep, specific capabilities are intentionally **not implemented** in Program 1.

### 1. `robots.txt` Path Rules (`Disallow`/`Allow`)

| Robots Capability | Status | Current Behavior |
|---|---|---|
| Fetch `/robots.txt` | ✅ Implemented | Seeded automatically for HTTP/HTTPS origins |
| Read `Sitemap:` directives | ✅ Implemented | Extracts declared sitemap URLs for discovery (RFC 9309) |
| Graceful 404 handling | ✅ Implemented | Missing `robots.txt` returns `NOT_FOUND` without health penalty |
| `Disallow` path enforcement | ❌ Not Implemented | Does not filter or block URL paths against `Disallow:` rules |
| `Allow` path enforcement | ❌ Not Implemented | Does not evaluate path-level allowances |
| `Crawl-delay` enforcement | ❌ Not Implemented | Does not parse or delay requests based on `Crawl-delay` directives |

> **Notice:** Program 1 parses `robots.txt` strictly for sitemap discovery; it does not evaluate path permission rules against user agents.

### 2. Anti-Bot Challenge & CAPTCHA Bypass

| Mechanism | Status | Notes |
|---|---|---|
| Browser fingerprint injection | ✅ Implemented | Injects realistic navigator/screen profiles via Crawlee/Browserforge |
| Proxy pool rotation & quarantine | ✅ Implemented | Rotates healthy proxies and quarantines failing ones |
| Per-domain session reuse | ✅ Implemented | Reuses browser identities across requests |
| Interactive CAPTCHA solving | ❌ Not Implemented | Does not solve reCAPTCHA, hCaptcha, or audio challenges |
| Cloudflare Turnstile bypass | ❌ Not Implemented | Does not bypass Cloudflare/Akamai managed challenge screens |
| Automated credential login | ❌ Not Implemented | Does not support form-based authentication or session farming |

If a site blocks access with an interactive CAPTCHA or HTTP 403 challenge, Program 1 **preserves the failure** as an acquisition event and continues.

### 3. Interactive Web Navigation

| Capability | Status | Notes |
|---|---|---|
| Headless DOM rendering | ✅ Implemented | Playwright renders client-side JS into static HTML |
| Dynamic SPA shell detection | ✅ Implemented | Heuristic detects client-rendered pages |
| UI button clicking / form fill | ❌ Not Implemented | Does not interact with UI elements or submit forms |
| Infinite-scroll emulation | ❌ Not Implemented | Does not scroll pages to trigger AJAX lazy-loading |
| Multi-step workflow navigation | ❌ Not Implemented | Does not step through wizards or multi-page forms |

---

## Setup & Requirements

### Prerequisites

- **Python 3.12+**
- [`uv`](https://docs.astral.sh/uv/) for Python package and environment management
- **PostgreSQL 16+** (with the `pgvector` extension enabled)
- **Chromium for Playwright** (required for browser acquisition escalation)
- **Docker** (optional, for local containerized PostgreSQL)

### Installation

```bash
# 1. Install dependencies
uv sync --dev

# 2. Install Chromium for Playwright browser escalation
uv run playwright install chromium

# 3. Start PostgreSQL with pgvector
docker compose up -d

# 4. Configure environment
cp .env.example .env
# Ensure DATABASE_URL matches your PostgreSQL connection

# 5. Apply database migrations
uv run kpi-crawler migrate
```

---

## Configuration

Settings are configured via environment variables (`kpi_crawler/config.py`):

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `LOG_LEVEL` | No | `INFO` | Standard Python logging level (`DEBUG`, `INFO`, `WARNING`, etc.) |
| `ARTIFACT_STORAGE_DIR` | No | `.data/artifacts` | Content-addressed storage root for raw files |
| `EXPORT_DIR` | No | `exports` | Output root directory for run export packages |
| `ACQUISITION_TIMEOUT_SECONDS` | No | `10` | Network request timeout in seconds |
| `ACQUISITION_RETRIES` | No | `2` | Number of retry attempts for failed requests |
| `MAX_ARTIFACT_BYTES` | No | `50000000` | Maximum artifact size in bytes (50 MB) |
| `ALLOWED_CONTENT_TYPES` | No | `application/pdf` | Comma-separated list of MIME types allowed for processing |
| `CRAWL_MAX_DEPTH` | No | `2` | Maximum crawl depth for link discovery |
| `CRAWL_MAX_ARTIFACTS` | No | `50` | Maximum number of artifacts to acquire per crawl run |

---

## CLI Reference & How to Run

### 1. Main Pipeline (`kpi-crawler`)

The primary CLI drives end-to-end processing, crawling, and exporting:

```bash
# Validate database connection and configuration boundary
uv run kpi-crawler start

# Apply pending SQL migrations
uv run kpi-crawler migrate

# Process a single URL or local file end-to-end (acquire, store, extract, persist)
uv run kpi-crawler process <source-url-or-file-path>

# Perform a bounded breadth-first crawl starting from a root URL
uv run kpi-crawler crawl <source-url>

# Export a completed run to a standalone directory (manifest, files, JSONL, report)
uv run kpi-crawler export <run-id>
```

### 2. Adaptive Acquisition Engine (`kpi-crawler-acquire`)

Runs the adaptive crawler featuring domain-level concurrency, adaptive backoff, proxy rotation, fingerprinting, and headless browser escalation:

```bash
# Basic run
uv run kpi-crawler-acquire <source-url>

# Verbose logging with attempt-level, proxy, and fingerprint output
uv run kpi-crawler-acquire --verbose <source-url>

# Custom depth, artifact, and concurrency limits
uv run kpi-crawler-acquire --max-depth 2 --max-artifacts 50 --max-concurrency 8 <source-url>

# Disable browser escalation (HTTP only)
uv run kpi-crawler-acquire --no-browser <source-url>
```

### 3. Semantic Retrieval Baseline (`kpi-crawler-retrieval`)

Executes an exact cosine similarity retrieval benchmark over stored evidence representations using Ollama embeddings:

```bash
# 1. Pull the embedding model in Ollama
ollama pull qwen3-embedding:4b

# 2. Run retrieval evaluation against a KPI dictionary
KPI_DICTIONARY_PATH=DOC-20260901-WA0019.xlsx uv run kpi-crawler-retrieval
```

*Optional environment overrides: `EMBEDDING_MODEL` (default: `qwen3-embedding:4b`), `EMBEDDING_ENDPOINT` (default: `http://localhost:11434/api/embeddings`), `EMBEDDING_DIMENSION` (default: `2560`), `RETRIEVAL_TOP_K` (default: `10`).*

---

## Testing

```bash
# Run unit tests (skips DB-dependent tests automatically when PostgreSQL is offline)
uv run pytest -q

# Run full test suite including PostgreSQL integration tests
docker compose up -d
uv run kpi-crawler migrate
uv run pytest -q

# Run a specific test module with verbose output
uv run pytest tests/test_acquisition_engine_storage.py -v
```

---

## Project Layout

```text
kpi_crawler/
├── config.py                 # Pydantic/environment settings
├── db.py                     # Database connection pool boundary
├── migrations.py             # Migration discovery and runner
├── migrations/               # Versioned SQL migration files
├── acquisition.py            # HTTP/file fetching, validation, and event persistence
├── discovery.py              # Pure HTML/sitemap/robots link parsers (no network I/O)
├── crawl.py                  # Breadth-first crawl orchestration engine
├── extraction.py             # Docling extraction wrapper (text, tables, visuals)
├── evidence_serialization.py # Canonical serialization and format versioning
├── pipeline.py               # Integrated acquire/store/extract workflow
├── export.py                 # Export engine (manifests, JSONL evidence, reports)
├── models.py                 # Core domain models and dataclasses
├── errors.py                 # Typed domain exception hierarchy
├── storage.py                # PostgreSQL repository layer
├── logging.py                # Structured logging configuration
├── cli.py                    # Main CLI entry point (`kpi-crawler`)
├── acquisition_engine/       # Adaptive crawler with session tracking & reporting
└── retrieval/                # Baseline semantic retrieval benchmarking tools
tests/                        # Unit and integration test suites
```
