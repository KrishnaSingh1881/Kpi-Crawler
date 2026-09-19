# Engineering rules

- Read the relevant requirements and existing implementation before editing.
- Keep the architecture simple, explicit, and limited to the current phase.
- Do not copy or refactor the legacy `university_kpi` repository.
- Do not implement future crawler, relevance, scoring, or intelligence features.
- Do not add abstractions without a current use.
- Preserve clear contracts between configuration, database access, migrations, and CLI code.
- Expected operational failures must be logged without exposing a traceback to CLI users.
- New behavior must have a focused automated check.
- Run tests and the applicable startup/database checks before handoff.
- Record completed work and unresolved issues in `progress.md`.

## How to run

### Prerequisites & Setup

1. **Install dependencies**:
   ```bash
   uv sync --dev
   ```
2. **Install Chromium for Playwright (browser acquisition)**:
   ```bash
   uv run playwright install chromium
   ```
3. **Start PostgreSQL (with pgvector)**:
   ```bash
   docker compose up -d
   ```
4. **Configure environment**:
   ```bash
   cp .env.example .env
   # Ensure DATABASE_URL is set in .env (e.g. postgresql://kpi_crawler:kpi_crawler@localhost:5432/kpi_crawler)
   ```
5. **Apply database migrations**:
   ```bash
   uv run kpi-crawler migrate
   ```

### Running Tests

```bash
# Run unit tests (automatically skips DB-dependent integration tests if DB is offline)
uv run pytest -q

# Run with coverage / specific test file
uv run pytest tests/test_acquisition_engine_cli.py -v
```

### 1. Main Pipeline (`kpi-crawler`)

- **Validate configuration & database boundary**:
  ```bash
  uv run kpi-crawler start
  ```
- **Process a single document end-to-end** (acquire, store, extract PDF evidence, persist to PostgreSQL):
  ```bash
  uv run kpi-crawler process <source-url-or-file-path>
  ```
- **Breadth-first crawl from a root URL**:
  ```bash
  uv run kpi-crawler crawl <source-url>
  ```
- **Export a completed run to human-readable artifacts** (README, manifest, JSONL evidence):
  ```bash
  uv run kpi-crawler export <run-id>
  ```

### 2. Program 1 Adaptive Acquisition Engine (`kpi-crawler-acquire`)

Runs the concurrent HTTP acquisition engine with adaptive concurrency/backoff, proxy pool, headless browser escalation (Playwright), and append-only evidence ledger:

```bash
# Basic run
uv run kpi-crawler-acquire <source-url>

# Verbose run with per-attempt request/session/proxy/fingerprint output
uv run kpi-crawler-acquire --verbose <source-url>

# Custom depth, artifact, and concurrency limits
uv run kpi-crawler-acquire --max-depth 2 --max-artifacts 50 --max-concurrency 8 <source-url>

# Disable headless browser escalation (HTTP only)
uv run kpi-crawler-acquire --no-browser <source-url>
```

### 3. Semantic Retrieval Baseline (`kpi-crawler-retrieval`)

Runs exact pgvector cosine similarity search over extracted evidence representations against the KPI dictionary:

1. Ensure Ollama is running with the embedding model:
   ```bash
   ollama pull qwen3-embedding:4b
   ```
2. Run the retrieval baseline:
   ```bash
   KPI_DICTIONARY_PATH=DOC-20260901-WA0019.xlsx uv run kpi-crawler-retrieval
   ```
   *Optional configuration overrides via environment variables:*
   - `EMBEDDING_MODEL`: defaults to `qwen3-embedding:4b`
   - `EMBEDDING_ENDPOINT`: defaults to `http://localhost:11434/api/embeddings`
   - `EMBEDDING_DIMENSION`: defaults to `2560`
   - `RETRIEVAL_TOP_K`: defaults to `10`
