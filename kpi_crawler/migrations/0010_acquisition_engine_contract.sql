-- Program 1's versioned acquisition contract and evidence ledger.
--
-- Deliberately a separate schema from `app`: this is the new acquisition
-- engine's own storage, independent of the existing single-threaded
-- crawler's app.artifacts/app.evidence tables. Program 2 (not yet built)
-- will depend on this schema's shape (contract.py mirrors it exactly), not
-- on app.* or on this engine's internals (sessions/proxies/adaptive policy).

CREATE SCHEMA IF NOT EXISTS acq;

CREATE TABLE acq.runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contract_version INTEGER NOT NULL,
    root_source_url TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING'
        CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE acq.artifacts (
    artifact_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contract_version INTEGER NOT NULL,
    run_id BIGINT NOT NULL REFERENCES acq.runs (id),
    source_url TEXT NOT NULL CHECK (length(source_url) > 0),
    canonical_url TEXT,
    discovered_from TEXT,
    fetched_at TIMESTAMPTZ NOT NULL,
    content_type TEXT,
    http_status INTEGER,
    acquisition_method TEXT NOT NULL CHECK (acquisition_method IN ('http', 'browser')),
    raw_location TEXT NOT NULL CHECK (length(raw_location) > 0),
    content_size BIGINT NOT NULL CHECK (content_size >= 0),
    checksum CHAR(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    encoding TEXT,
    final_url TEXT,
    redirect_chain JSONB NOT NULL DEFAULT '[]'::jsonb,
    session_id TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX acq_artifacts_run_idx ON acq.artifacts (run_id);
CREATE INDEX acq_artifacts_checksum_idx ON acq.artifacts (checksum);

-- Immutable, like app.artifacts: an acquired artifact's record is a fact
-- about what was fetched and must never be silently rewritten.
CREATE FUNCTION acq.reject_artifact_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'acq.artifacts rows are immutable';
END;
$$;

CREATE TRIGGER acq_artifacts_no_update
BEFORE UPDATE ON acq.artifacts
FOR EACH ROW EXECUTE FUNCTION acq.reject_artifact_mutation();

CREATE TRIGGER acq_artifacts_no_delete
BEFORE DELETE ON acq.artifacts
FOR EACH ROW EXECUTE FUNCTION acq.reject_artifact_mutation();

-- The Evidence Ledger: one row per acquisition attempt, success or failure.
-- Never updated after insert, never merged, never overwritten by a later
-- attempt at the same URL.
CREATE TABLE acq.attempts (
    attempt_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES acq.runs (id),
    artifact_id BIGINT REFERENCES acq.artifacts (artifact_id),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    url TEXT NOT NULL CHECK (length(url) > 0),
    domain TEXT NOT NULL,
    acquisition_method TEXT NOT NULL CHECK (acquisition_method IN ('http', 'browser')),
    session_id TEXT,
    proxy_id TEXT,
    proxy_status TEXT,
    http_status INTEGER,
    latency_ms DOUBLE PRECISION,
    retry_number INTEGER NOT NULL DEFAULT 0,
    retry_budget INTEGER,
    timeout_seconds DOUBLE PRECISION,
    backoff_applied_seconds DOUBLE PRECISION,
    concurrency_at_attempt INTEGER,
    rate_limit_detected BOOLEAN NOT NULL DEFAULT false,
    failure_classification TEXT,
    adaptive_decision TEXT,
    final_result TEXT NOT NULL,
    error_message TEXT
);

CREATE INDEX acq_attempts_run_idx ON acq.attempts (run_id, occurred_at);
CREATE INDEX acq_attempts_artifact_idx ON acq.attempts (artifact_id);
CREATE INDEX acq_attempts_url_idx ON acq.attempts (run_id, url);

CREATE TABLE acq.adaptive_decisions (
    decision_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES acq.runs (id),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    domain TEXT,
    decision_type TEXT NOT NULL CHECK (length(decision_type) > 0),
    reason TEXT NOT NULL,
    before_value TEXT,
    after_value TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX acq_adaptive_decisions_run_idx ON acq.adaptive_decisions (run_id, occurred_at);
