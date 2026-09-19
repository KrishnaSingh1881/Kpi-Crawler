-- Adds the INTERRUPTED run-level status (contract.py's AcquisitionState):
-- an operator-stopped run (e.g. Ctrl+C) that ran to completion of its
-- in-flight work but scheduled no more — distinct from FAILED (nothing
-- succeeded) and PARTIAL (ran to completion with mixed results).

ALTER TABLE acq.runs DROP CONSTRAINT runs_status_check;

ALTER TABLE acq.runs
    ADD CONSTRAINT runs_status_check
    CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'INTERRUPTED'));
