CREATE TABLE app.acquisition_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed')),
    root_source_url TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    CHECK (
        (status = 'running' AND completed_at IS NULL)
        OR (status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)
    ),
    CHECK (status <> 'failed' OR error_message IS NOT NULL)
);

CREATE TABLE app.artifacts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256 CHAR(64) NOT NULL UNIQUE
        CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    raw_storage_ref TEXT NOT NULL CHECK (length(raw_storage_ref) > 0),
    source_url TEXT,
    source_type TEXT,
    content_type TEXT,
    retrieved_at TIMESTAMPTZ NOT NULL,
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app.acquisition_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES app.acquisition_runs (id),
    parent_event_id BIGINT,
    event_type TEXT NOT NULL CHECK (length(event_type) > 0),
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    source_url TEXT,
    source_type TEXT,
    content_type TEXT,
    retrieved_at TIMESTAMPTZ,
    error_message TEXT,
    artifact_id BIGINT REFERENCES app.artifacts (id),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, run_id),
    FOREIGN KEY (parent_event_id, run_id)
        REFERENCES app.acquisition_events (id, run_id),
    CHECK (status <> 'failed' OR error_message IS NOT NULL)
);

CREATE INDEX acquisition_runs_status_idx
    ON app.acquisition_runs (status);
CREATE INDEX acquisition_events_run_idx
    ON app.acquisition_events (run_id, occurred_at, id);
CREATE INDEX acquisition_events_parent_idx
    ON app.acquisition_events (parent_event_id);
CREATE INDEX acquisition_events_source_url_idx
    ON app.acquisition_events (source_url);
CREATE INDEX acquisition_events_artifact_idx
    ON app.acquisition_events (artifact_id);
CREATE INDEX artifacts_source_url_idx
    ON app.artifacts (source_url);

CREATE FUNCTION app.reject_artifact_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'artifacts are immutable';
END;
$$;

CREATE TRIGGER artifacts_no_update
BEFORE UPDATE ON app.artifacts
FOR EACH ROW EXECUTE FUNCTION app.reject_artifact_mutation();

CREATE TRIGGER artifacts_no_delete
BEFORE DELETE ON app.artifacts
FOR EACH ROW EXECUTE FUNCTION app.reject_artifact_mutation();
