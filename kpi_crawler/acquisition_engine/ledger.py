"""The Acquisition Evidence Ledger: persistence for runs, artifacts, attempts,
and adaptive decisions.

Separation of concerns: the acquisition engine's job is "acquire this
source"; this module's job is "record exactly what happened while attempting
to acquire this source." The engine calls this module; this module never
calls back into the engine.
"""

from datetime import datetime, timezone
import threading
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from .contract import (
    CONTRACT_VERSION,
    AcquisitionState,
    AdaptiveDecisionRecord,
    ArtifactRecord,
    AttemptRecord,
    RedirectHop,
    RunSummary,
    derive_run_status,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceLedger:
    """Records acquisition attempts, artifacts, and adaptive decisions for one
    connection/transaction scope. Every write here is append-only.
    """

    def __init__(self, conn: Connection[Any]):
        """`conn` is put into autocommit mode: every attempt/artifact/decision
        must be durable the instant it's recorded (a crash mid-run must never
        lose evidence for what already happened), and multiple acquisition
        worker threads share this one connection under `_lock` below —
        psycopg connections are not safe for concurrent use otherwise.
        """
        self._conn = conn
        self._conn.autocommit = True
        self._lock = threading.Lock()

    def _execute(self, sql: str, params: tuple[Any, ...]):
        """Every statement goes through here: a single lock around each
        individual execute+fetch call is what makes it safe for multiple
        acquisition worker threads to share one psycopg connection.
        """
        with self._lock:
            return self._conn.execute(sql, params)

    def create_run(self, root_source_url: str | None) -> int:
        row = self._execute(
            "INSERT INTO acq.runs (contract_version, root_source_url) VALUES (%s, %s) RETURNING id",
            (CONTRACT_VERSION, root_source_url),
        ).fetchone()
        return row[0]

    def complete_run(self, run_id: int, status: AcquisitionState, summary: dict[str, Any]) -> None:
        self._execute(
            "UPDATE acq.runs SET status = %s, completed_at = %s, summary = %s WHERE id = %s",
            (status.value, _utc_now(), Jsonb(summary), run_id),
        )

    def record_artifact(
        self,
        *,
        run_id: int,
        source_url: str,
        canonical_url: str | None,
        discovered_from: str | None,
        fetched_at: datetime,
        content_type: str | None,
        http_status: int | None,
        acquisition_method: str,
        raw_location: str,
        content_size: int,
        checksum: str,
        encoding: str | None,
        final_url: str | None,
        redirect_chain: tuple[RedirectHop, ...],
        session_id: str | None,
    ) -> ArtifactRecord:
        row = self._execute(
            """
            INSERT INTO acq.artifacts
                (contract_version, run_id, source_url, canonical_url, discovered_from,
                 fetched_at, content_type, http_status, acquisition_method, raw_location,
                 content_size, checksum, encoding, final_url, redirect_chain, session_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING artifact_id
            """,
            (
                CONTRACT_VERSION,
                run_id,
                source_url,
                canonical_url,
                discovered_from,
                fetched_at,
                content_type,
                http_status,
                acquisition_method,
                raw_location,
                content_size,
                checksum,
                encoding,
                final_url,
                Jsonb([{"url": hop.url, "status": hop.status} for hop in redirect_chain]),
                session_id,
                AcquisitionState.SUCCESS.value,
            ),
        ).fetchone()
        return ArtifactRecord(
            contract_version=CONTRACT_VERSION,
            artifact_id=row[0],
            run_id=run_id,
            source_url=source_url,
            canonical_url=canonical_url,
            discovered_from=discovered_from,
            fetched_at=fetched_at,
            content_type=content_type,
            http_status=http_status,
            acquisition_method=acquisition_method,
            raw_location=raw_location,
            content_size=content_size,
            checksum=checksum,
            encoding=encoding,
            final_url=final_url,
            redirect_chain=redirect_chain,
            session_id=session_id,
            status=AcquisitionState.SUCCESS,
        )

    def record_attempt(
        self,
        *,
        run_id: int,
        url: str,
        domain: str,
        acquisition_method: str,
        session_id: str | None,
        proxy_id: str | None,
        proxy_status: str | None,
        http_status: int | None,
        latency_ms: float | None,
        retry_number: int,
        retry_budget: int | None,
        timeout_seconds: float | None,
        backoff_applied_seconds: float | None,
        concurrency_at_attempt: int | None,
        rate_limit_detected: bool,
        failure_classification: str | None,
        adaptive_decision: str | None,
        final_result: AcquisitionState,
        artifact_id: int | None,
        error_message: str | None = None,
    ) -> AttemptRecord:
        occurred_at = _utc_now()
        row = self._execute(
            """
            INSERT INTO acq.attempts
                (run_id, artifact_id, occurred_at, url, domain, acquisition_method, session_id,
                 proxy_id, proxy_status, http_status, latency_ms, retry_number, retry_budget,
                 timeout_seconds, backoff_applied_seconds, concurrency_at_attempt,
                 rate_limit_detected, failure_classification, adaptive_decision, final_result,
                 error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING attempt_id
            """,
            (
                run_id,
                artifact_id,
                occurred_at,
                url,
                domain,
                acquisition_method,
                session_id,
                proxy_id,
                proxy_status,
                http_status,
                latency_ms,
                retry_number,
                retry_budget,
                timeout_seconds,
                backoff_applied_seconds,
                concurrency_at_attempt,
                rate_limit_detected,
                failure_classification,
                adaptive_decision,
                final_result.value,
                error_message,
            ),
        ).fetchone()
        return AttemptRecord(
            run_id=run_id,
            attempt_id=row[0],
            occurred_at=occurred_at,
            url=url,
            domain=domain,
            acquisition_method=acquisition_method,
            session_id=session_id,
            proxy_id=proxy_id,
            proxy_status=proxy_status,
            http_status=http_status,
            latency_ms=latency_ms,
            retry_number=retry_number,
            retry_budget=retry_budget,
            timeout_seconds=timeout_seconds,
            backoff_applied_seconds=backoff_applied_seconds,
            concurrency_at_attempt=concurrency_at_attempt,
            rate_limit_detected=rate_limit_detected,
            failure_classification=failure_classification,
            adaptive_decision=adaptive_decision,
            final_result=final_result,
            artifact_id=artifact_id,
            error_message=error_message,
        )

    def record_adaptive_decision(
        self,
        *,
        run_id: int,
        domain: str | None,
        decision_type: str,
        reason: str,
        before_value: str | None,
        after_value: str | None,
        details: dict[str, Any] | None = None,
    ) -> AdaptiveDecisionRecord:
        occurred_at = _utc_now()
        row = self._execute(
            """
            INSERT INTO acq.adaptive_decisions
                (run_id, occurred_at, domain, decision_type, reason, before_value, after_value, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING decision_id
            """,
            (run_id, occurred_at, domain, decision_type, reason, before_value, after_value, Jsonb(details or {})),
        ).fetchone()
        return AdaptiveDecisionRecord(
            run_id=run_id,
            decision_id=row[0],
            occurred_at=occurred_at,
            domain=domain,
            decision_type=decision_type,
            reason=reason,
            before_value=before_value,
            after_value=after_value,
            details=details or {},
        )

    def attempts_for_run(self, run_id: int) -> list[AttemptRecord]:
        rows = self._execute(
            """
            SELECT run_id, attempt_id, occurred_at, url, domain, acquisition_method, session_id,
                   proxy_id, proxy_status, http_status, latency_ms, retry_number, retry_budget,
                   timeout_seconds, backoff_applied_seconds, concurrency_at_attempt,
                   rate_limit_detected, failure_classification, adaptive_decision, final_result,
                   artifact_id, error_message
            FROM acq.attempts WHERE run_id = %s ORDER BY occurred_at, attempt_id
            """,
            (run_id,),
        ).fetchall()
        results = []
        for row in rows:
            values = list(row)
            values[19] = AcquisitionState(values[19])
            results.append(AttemptRecord(*values))
        return results

    def artifacts_for_run(self, run_id: int) -> list[ArtifactRecord]:
        rows = self._execute(
            """
            SELECT contract_version, artifact_id, run_id, source_url, canonical_url, discovered_from,
                   fetched_at, content_type, http_status, acquisition_method, raw_location,
                   content_size, checksum, encoding, final_url, redirect_chain, session_id, status
            FROM acq.artifacts WHERE run_id = %s ORDER BY artifact_id
            """,
            (run_id,),
        ).fetchall()
        results = []
        for row in rows:
            values = list(row)
            values[15] = tuple(RedirectHop(hop["url"], hop["status"]) for hop in values[15])
            values[17] = AcquisitionState(values[17])
            results.append(ArtifactRecord(*values))
        return results

    def adaptive_decisions_for_run(self, run_id: int) -> list[AdaptiveDecisionRecord]:
        rows = self._execute(
            """
            SELECT run_id, decision_id, occurred_at, domain, decision_type, reason,
                   before_value, after_value, details
            FROM acq.adaptive_decisions WHERE run_id = %s ORDER BY occurred_at, decision_id
            """,
            (run_id,),
        ).fetchall()
        return [AdaptiveDecisionRecord(*row) for row in rows]

    def summarize_run(self, run_id: int) -> RunSummary:
        """Per-URL last-attempt rollup: a URL's final outcome is its most
        recent attempt's result, matching how a caller experiences the run
        ("what ultimately happened to this URL"), while every earlier
        attempt stays in the ledger untouched.
        """
        run_row = self._execute(
            "SELECT contract_version, root_source_url, started_at, completed_at, status FROM acq.runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise LookupError(f"acquisition run {run_id} does not exist")
        contract_version, root_source_url, started_at, completed_at, status = run_row

        attempts = self.attempts_for_run(run_id)
        last_by_url: dict[str, AttemptRecord] = {}
        for attempt in attempts:
            last_by_url[attempt.url] = attempt  # attempts_for_run is time-ordered

        final_results = {url: attempt.final_result for url, attempt in last_by_url.items()}
        by_state: dict[str, int] = {}
        for state in final_results.values():
            by_state[state.value] = by_state.get(state.value, 0) + 1

        return RunSummary(
            run_id=run_id,
            contract_version=contract_version,
            root_source_url=root_source_url,
            started_at=started_at,
            completed_at=completed_at,
            status=AcquisitionState(status) if status != "RUNNING" else derive_run_status(final_results),
            # `discovered` is corrected by the engine after this call to the
            # true link-discovery count; the ledger alone only knows about
            # URLs that were actually attempted, so it starts equal to
            # `attempted` here (accurate for a caller that queries the ledger
            # directly, e.g. tests, without going through the engine).
            discovered=len(last_by_url),
            attempted=len(last_by_url),
            acquired=sum(1 for s in final_results.values() if s == AcquisitionState.SUCCESS),
            partial=0,
            failed=sum(1 for s in final_results.values() if s != AcquisitionState.SUCCESS),
            # A retry is any attempt beyond a URL's first (retry_number > 0)
            # — NOT a sum of retry_number values, which would count a URL's
            # 4th attempt (retry_number=3) as "3 retries" on its own and
            # inflate the total (e.g. attempts numbered 0,1,2,3 is 3 retries
            # performed, not 0+1+2+3=6).
            retries=sum(1 for a in attempts if a.retry_number > 0),
            rate_limited=sum(1 for a in attempts if a.rate_limit_detected),
            browser_pages=sum(1 for a in attempts if a.acquisition_method == "browser"),
            proxy_failures=sum(1 for a in attempts if a.proxy_status == "unhealthy"),
            by_state=by_state,
        )
