CREATE TABLE app.extractions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    artifact_id BIGINT NOT NULL REFERENCES app.artifacts (id),
    extractor TEXT NOT NULL CHECK (length(extractor) > 0),
    extractor_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE app.evidence
    ADD COLUMN extraction_id BIGINT REFERENCES app.extractions (id),
    ADD COLUMN evidence_type TEXT NOT NULL DEFAULT 'text'
        CHECK (evidence_type IN ('text', 'table', 'visual')),
    ADD COLUMN structure JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX extractions_artifact_idx ON app.extractions (artifact_id, created_at);
CREATE INDEX evidence_extraction_idx ON app.evidence (extraction_id);
CREATE INDEX evidence_structure_idx ON app.evidence USING GIN (structure);
