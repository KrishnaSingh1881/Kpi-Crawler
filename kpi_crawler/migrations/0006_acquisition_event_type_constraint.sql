ALTER TABLE app.acquisition_events
    ADD CONSTRAINT acquisition_events_event_type_allowed_check
    CHECK (event_type IN ('acquisition', 'extraction', 'discovery'));
