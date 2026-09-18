CREATE UNIQUE INDEX extractions_succeeded_artifact_idx
    ON app.extractions (artifact_id)
    WHERE status = 'succeeded';
