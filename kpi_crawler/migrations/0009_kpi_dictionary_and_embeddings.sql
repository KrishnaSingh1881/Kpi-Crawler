-- KPI dictionary (loaded from an external, versioned xlsx source of record) and
-- the embedding tables backing the semantic retrieval baseline. Embeddings are
-- kept out of app.evidence entirely: they are a derived, model-specific,
-- re-computable representation, not part of the canonical evidence record.

CREATE TABLE app.kpis (
    kpi_code TEXT PRIMARY KEY CHECK (length(kpi_code) > 0),
    domain TEXT,
    parameter TEXT,
    sub_parameter TEXT,
    variable_name TEXT NOT NULL CHECK (length(variable_name) > 0),
    definition TEXT NOT NULL CHECK (length(definition) > 0),
    unit TEXT,
    data_type TEXT,
    formula TEXT,
    primary_source TEXT,
    benchmark_direction TEXT,
    priority_12m TEXT,
    source_file TEXT NOT NULL CHECK (length(source_file) > 0),
    source_row JSONB NOT NULL DEFAULT '{}'::jsonb,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (kpi, embedding model, representation). "representation" is
-- always the fixed name+definition construction for this baseline, but the
-- column exists so a future, differently-configured KPI representation never
-- collides with this one under the same model identifier.
CREATE TABLE app.kpi_embeddings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kpi_code TEXT NOT NULL REFERENCES app.kpis (kpi_code),
    representation TEXT NOT NULL CHECK (length(representation) > 0),
    model_identifier TEXT NOT NULL CHECK (length(model_identifier) > 0),
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    embedding VECTOR(2560) NOT NULL,
    normalized BOOLEAN NOT NULL,
    instruction TEXT,
    embedded_text_checksum TEXT NOT NULL CHECK (length(embedded_text_checksum) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kpi_code, model_identifier, representation)
);

-- One row per (evidence, representation, embedding model). Representation is
-- exactly one of the two existing persisted evidence fields ("A" =
-- canonical_text, "B" = supporting_context) — never a derived/rewritten text.
CREATE TABLE app.evidence_embeddings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evidence_id BIGINT NOT NULL REFERENCES app.evidence (id),
    representation TEXT NOT NULL CHECK (representation IN ('canonical_text', 'supporting_context')),
    model_identifier TEXT NOT NULL CHECK (length(model_identifier) > 0),
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    embedding VECTOR(2560) NOT NULL,
    normalized BOOLEAN NOT NULL,
    instruction TEXT,
    canonical_serializer_version TEXT,
    embedded_text_checksum TEXT NOT NULL CHECK (length(embedded_text_checksum) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (evidence_id, representation, model_identifier)
);

CREATE INDEX evidence_embeddings_evidence_idx ON app.evidence_embeddings (evidence_id);
CREATE INDEX evidence_embeddings_representation_idx ON app.evidence_embeddings (representation, model_identifier);
CREATE INDEX kpi_embeddings_kpi_idx ON app.kpi_embeddings (kpi_code);
