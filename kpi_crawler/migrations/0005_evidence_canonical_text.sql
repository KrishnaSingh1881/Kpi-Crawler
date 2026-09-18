ALTER TABLE app.evidence
    ADD COLUMN canonical_text TEXT NOT NULL CHECK (length(canonical_text) > 0),
    ADD COLUMN canonical_serializer_version TEXT NOT NULL CHECK (length(canonical_serializer_version) > 0);
