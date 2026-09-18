CREATE TABLE app.evidence (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    artifact_id BIGINT NOT NULL REFERENCES app.artifacts (id),
    claim TEXT NOT NULL CHECK (length(claim) > 0),
    value TEXT NOT NULL CHECK (length(value) > 0),
    supporting_context TEXT NOT NULL CHECK (length(supporting_context) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, artifact_id)
);

CREATE TABLE app.evidence_provenance (
    evidence_id BIGINT PRIMARY KEY REFERENCES app.evidence (id),
    artifact_id BIGINT NOT NULL REFERENCES app.artifacts (id),
    source_url TEXT NOT NULL CHECK (length(source_url) > 0),
    page_number INTEGER CHECK (page_number IS NULL OR page_number > 0),
    location TEXT,
    extractor TEXT NOT NULL CHECK (length(extractor) > 0),
    extractor_version TEXT,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (evidence_id, artifact_id),
    FOREIGN KEY (evidence_id, artifact_id)
        REFERENCES app.evidence (id, artifact_id)
);

CREATE INDEX evidence_artifact_idx
    ON app.evidence (artifact_id);
CREATE INDEX evidence_provenance_source_url_idx
    ON app.evidence_provenance (source_url);
