import os
import threading
import unittest

import psycopg

from kpi_crawler.acquisition_engine.contract import AcquisitionState, RedirectHop
from kpi_crawler.acquisition_engine.ledger import EvidenceLedger
from kpi_crawler.migrations import migrate

DATABASE_URL = os.environ.get("DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is required for PostgreSQL integration tests")
class EvidenceLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate(DATABASE_URL)

    def _ledger(self) -> tuple[EvidenceLedger, psycopg.Connection]:
        conn = psycopg.connect(DATABASE_URL)
        return EvidenceLedger(conn), conn

    def test_create_and_complete_run(self):
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")
        ledger.complete_run(run_id, AcquisitionState.SUCCESS, {"discovered": 1})
        row = conn.execute("SELECT status, root_source_url FROM acq.runs WHERE id = %s", (run_id,)).fetchone()
        self.assertEqual(row, ("SUCCESS", "https://example.edu"))

    def test_record_artifact_roundtrip_including_redirect_chain(self):
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")
        artifact = ledger.record_artifact(
            run_id=run_id, source_url="https://example.edu/a", canonical_url=None,
            discovered_from="https://example.edu", fetched_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            content_type="text/html", http_status=200, acquisition_method="http",
            raw_location="/tmp/x.raw", content_size=123, checksum="a" * 64, encoding="utf-8",
            final_url="https://example.edu/a/", redirect_chain=(RedirectHop("https://example.edu/a/", 301),),
            session_id="session_1",
        )
        self.assertEqual(artifact.status, AcquisitionState.SUCCESS)
        fetched = ledger.artifacts_for_run(run_id)
        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0].redirect_chain, (RedirectHop("https://example.edu/a/", 301),))
        self.assertEqual(fetched[0].checksum, "a" * 64)

    def test_artifacts_are_immutable(self):
        ledger, conn = self._ledger()
        import datetime as dt

        run_id = ledger.create_run("https://example.edu")
        artifact = ledger.record_artifact(
            run_id=run_id, source_url="https://example.edu/a", canonical_url=None, discovered_from=None,
            fetched_at=dt.datetime.now(dt.timezone.utc), content_type="text/html", http_status=200,
            acquisition_method="http", raw_location="/tmp/x.raw", content_size=1, checksum="b" * 64,
            encoding=None, final_url=None, redirect_chain=(), session_id=None,
        )
        with self.assertRaises(psycopg.Error):
            conn.execute("UPDATE acq.artifacts SET content_size = 999 WHERE artifact_id = %s", (artifact.artifact_id,))

    def test_record_attempt_preserves_failure_evidence_never_overwritten(self):
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")
        for retry in range(4):
            ledger.record_attempt(
                run_id=run_id, url="https://example.edu/report", domain="example.edu",
                acquisition_method="http", session_id="session_1", proxy_id=None, proxy_status=None,
                http_status=429, latency_ms=10.0, retry_number=retry, retry_budget=4,
                timeout_seconds=10.0, backoff_applied_seconds=1.0 * (2**retry), concurrency_at_attempt=8 // (retry + 1),
                rate_limit_detected=True, failure_classification="http_429",
                adaptive_decision="concurrency_reduced" if retry == 0 else None,
                final_result=AcquisitionState.RATE_LIMITED, artifact_id=None,
            )
        attempts = ledger.attempts_for_run(run_id)
        self.assertEqual(len(attempts), 4)
        self.assertEqual([a.retry_number for a in attempts], [0, 1, 2, 3])
        self.assertTrue(all(a.final_result == AcquisitionState.RATE_LIMITED for a in attempts))

    def test_summarize_run_partial_when_some_urls_succeed_and_some_fail(self):
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")
        ledger.record_attempt(
            run_id=run_id, url="https://example.edu/ok", domain="example.edu", acquisition_method="http",
            session_id="s1", proxy_id=None, proxy_status=None, http_status=200, latency_ms=5.0,
            retry_number=0, retry_budget=3, timeout_seconds=10.0, backoff_applied_seconds=None,
            concurrency_at_attempt=8, rate_limit_detected=False, failure_classification=None,
            adaptive_decision=None, final_result=AcquisitionState.SUCCESS, artifact_id=None,
        )
        ledger.record_attempt(
            run_id=run_id, url="https://example.edu/denied", domain="example.edu", acquisition_method="http",
            session_id="s2", proxy_id=None, proxy_status=None, http_status=403, latency_ms=5.0,
            retry_number=0, retry_budget=3, timeout_seconds=10.0, backoff_applied_seconds=None,
            concurrency_at_attempt=8, rate_limit_detected=False, failure_classification="http_403",
            adaptive_decision=None, final_result=AcquisitionState.ACCESS_DENIED, artifact_id=None,
        )
        ledger.complete_run(run_id, AcquisitionState.PARTIAL, {})
        summary = ledger.summarize_run(run_id)
        self.assertEqual(summary.discovered, 2)
        self.assertEqual(summary.attempted, 2)
        self.assertEqual(summary.acquired, 1)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.attempted, summary.acquired + summary.failed)
        self.assertEqual(summary.status, AcquisitionState.PARTIAL)

    def test_summarize_run_uses_most_recent_attempt_per_url(self):
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")
        ledger.record_attempt(
            run_id=run_id, url="https://example.edu/x", domain="example.edu", acquisition_method="http",
            session_id="s1", proxy_id=None, proxy_status=None, http_status=429, latency_ms=5.0,
            retry_number=0, retry_budget=3, timeout_seconds=10.0, backoff_applied_seconds=1.0,
            concurrency_at_attempt=8, rate_limit_detected=True, failure_classification="http_429",
            adaptive_decision=None, final_result=AcquisitionState.RATE_LIMITED, artifact_id=None,
        )
        ledger.record_attempt(
            run_id=run_id, url="https://example.edu/x", domain="example.edu", acquisition_method="http",
            session_id="s1", proxy_id=None, proxy_status=None, http_status=200, latency_ms=5.0,
            retry_number=1, retry_budget=3, timeout_seconds=10.0, backoff_applied_seconds=None,
            concurrency_at_attempt=4, rate_limit_detected=False, failure_classification=None,
            adaptive_decision=None, final_result=AcquisitionState.SUCCESS, artifact_id=None,
        )
        summary = ledger.summarize_run(run_id)
        self.assertEqual(summary.discovered, 1)
        self.assertEqual(summary.attempted, 1)
        self.assertEqual(summary.acquired, 1)  # final attempt succeeded, even though the first didn't
        self.assertEqual(summary.status, AcquisitionState.SUCCESS)

    def test_summarize_run_counts_retries_not_a_sum_of_retry_numbers(self):
        """A URL retried 3 times (attempts at retry_number 0,1,2,3) performed
        3 retries — not sum(0,1,2,3)=6, which is what a naive implementation
        would compute.
        """
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")
        for retry_number, result in enumerate(
            [AcquisitionState.RATE_LIMITED, AcquisitionState.RATE_LIMITED, AcquisitionState.RATE_LIMITED, AcquisitionState.SUCCESS]
        ):
            ledger.record_attempt(
                run_id=run_id, url="https://example.edu/flaky", domain="example.edu", acquisition_method="http",
                session_id="s1", proxy_id=None, proxy_status=None, http_status=200, latency_ms=5.0,
                retry_number=retry_number, retry_budget=4, timeout_seconds=10.0, backoff_applied_seconds=None,
                concurrency_at_attempt=8, rate_limit_detected=False, failure_classification=None,
                adaptive_decision=None, final_result=result, artifact_id=None,
            )
        summary = ledger.summarize_run(run_id)
        self.assertEqual(summary.retries, 3)

    def test_record_adaptive_decision_roundtrip(self):
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")
        decision = ledger.record_adaptive_decision(
            run_id=run_id, domain="example.edu", decision_type="concurrency_reduced",
            reason="429 detected", before_value="8", after_value="4", details={"trigger": "429"},
        )
        self.assertEqual(decision.decision_type, "concurrency_reduced")
        row = conn.execute(
            "SELECT decision_type, before_value, after_value FROM acq.adaptive_decisions WHERE decision_id = %s",
            (decision.decision_id,),
        ).fetchone()
        self.assertEqual(row, ("concurrency_reduced", "8", "4"))

    def test_adaptive_decisions_for_run_returns_them_in_order(self):
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")
        ledger.record_adaptive_decision(
            run_id=run_id, domain="example.edu", decision_type="concurrency_reduced",
            reason="429 detected", before_value="8", after_value="4",
        )
        ledger.record_adaptive_decision(
            run_id=run_id, domain="example.edu", decision_type="proxy_quarantined",
            reason="failure threshold reached", before_value="healthy", after_value="quarantined",
        )
        decisions = ledger.adaptive_decisions_for_run(run_id)
        self.assertEqual([d.decision_type for d in decisions], ["concurrency_reduced", "proxy_quarantined"])

    def test_concurrent_attempt_recording_from_multiple_threads_is_safe(self):
        ledger, conn = self._ledger()
        run_id = ledger.create_run("https://example.edu")

        def record(i: int) -> None:
            ledger.record_attempt(
                run_id=run_id, url=f"https://example.edu/{i}", domain="example.edu",
                acquisition_method="http", session_id=f"s{i}", proxy_id=None, proxy_status=None,
                http_status=200, latency_ms=1.0, retry_number=0, retry_budget=3,
                timeout_seconds=10.0, backoff_applied_seconds=None, concurrency_at_attempt=8,
                rate_limit_detected=False, failure_classification=None, adaptive_decision=None,
                final_result=AcquisitionState.SUCCESS, artifact_id=None,
            )

        threads = [threading.Thread(target=record, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        attempts = ledger.attempts_for_run(run_id)
        self.assertEqual(len(attempts), 20)
        self.assertEqual(len({a.attempt_id for a in attempts}), 20)


if __name__ == "__main__":
    unittest.main()
