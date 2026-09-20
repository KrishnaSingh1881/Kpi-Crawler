"""Unit tests for `engine.classify_http_outcome`, the pure function that maps
whatever crawlee's `HttpCrawler` surfaces for one acquisition attempt onto our
own `AcquisitionState` contract.

This is the highest-risk part of the Crawlee port: Stage 0 established that a
404 is never delivered to the normal request handler at all — crawlee raises
`HttpClientStatusCodeError` before the handler runs, and 401/403/429 raise
`SessionError` instead (with no `.status_code` attribute), since they are
"blocked" statuses that trigger session rotation. Getting any of these
confused with a genuine connectivity failure would silently corrupt the
evidence ledger's failure classification, so every case here is exercised
directly against constructed crawlee exception objects — no HTTP server,
no crawler, no database.
"""

import asyncio
import unittest

from crawlee.errors import HttpClientStatusCodeError, HttpStatusCodeError, ProxyError, SessionError

from kpi_crawler.acquisition_engine.contract import AcquisitionState
from kpi_crawler.acquisition_engine.engine import classify_http_outcome


class SuccessTests(unittest.TestCase):
    def test_no_error_is_success(self):
        state, classification, http_status = classify_http_outcome(None)
        self.assertEqual(state, AcquisitionState.SUCCESS)
        self.assertIsNone(classification)
        self.assertIsNone(http_status)


class NotFoundTests(unittest.TestCase):
    def test_404_client_status_error_is_not_found(self):
        error = HttpClientStatusCodeError("Client error status code returned", 404)
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.NOT_FOUND)
        self.assertEqual(classification, "http_404")
        self.assertEqual(http_status, 404)

    def test_404_is_never_confused_with_network_error(self):
        error = HttpClientStatusCodeError("Client error status code returned", 404)
        state, _, _ = classify_http_outcome(error)
        self.assertNotEqual(state, AcquisitionState.NETWORK_ERROR)


class AccessDeniedTests(unittest.TestCase):
    def test_403_blocked_session_error_is_access_denied(self):
        """403 is a default-blocked status code, so crawlee raises `SessionError`
        with only a message ("Assuming the session is blocked based on HTTP
        status code 403") — not `HttpClientStatusCodeError` — before the
        handler ever runs (verified against crawlee 1.10.1 source).
        """
        error = SessionError("Assuming the session is blocked based on HTTP status code 403")
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.ACCESS_DENIED)
        self.assertEqual(classification, "http_403")
        self.assertEqual(http_status, 403)

    def test_401_blocked_session_error_is_access_denied(self):
        error = SessionError("Assuming the session is blocked based on HTTP status code 401")
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.ACCESS_DENIED)
        self.assertEqual(classification, "http_401")
        self.assertEqual(http_status, 401)

    def test_403_as_a_plain_client_status_error_is_still_access_denied(self):
        """Defensive: if `ignore_http_error_status_codes`/blocked-list config
        ever changes such that 403 reaches `_raise_for_error_status_code`
        instead of session-blocking, the mapping must still be correct.
        """
        error = HttpClientStatusCodeError("Client error status code returned", 403)
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.ACCESS_DENIED)
        self.assertEqual(classification, "http_403")
        self.assertEqual(http_status, 403)


class RateLimitedTests(unittest.TestCase):
    def test_429_blocked_session_error_is_rate_limited(self):
        error = SessionError("Assuming the session is blocked based on HTTP status code 429")
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.RATE_LIMITED)
        self.assertEqual(classification, "http_429")
        self.assertEqual(http_status, 429)

    def test_429_as_a_plain_client_status_error_is_still_rate_limited(self):
        error = HttpClientStatusCodeError("Client error status code returned", 429)
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.RATE_LIMITED)
        self.assertEqual(classification, "http_429")
        self.assertEqual(http_status, 429)


class TimeoutTests(unittest.TestCase):
    def test_asyncio_timeout_error_is_timeout(self):
        state, classification, http_status = classify_http_outcome(asyncio.TimeoutError())
        self.assertEqual(state, AcquisitionState.TIMEOUT)
        self.assertEqual(classification, "timeout")
        self.assertIsNone(http_status)


class ConnectionErrorTests(unittest.TestCase):
    def test_generic_exception_is_network_error(self):
        """A raw transport failure (DNS resolution, connection refused, reset,
        ...) that crawlee's httpx client didn't recognize as blocked/timeout/
        proxy-related — the catch-all genuine connectivity fault.
        """
        state, classification, http_status = classify_http_outcome(ConnectionError("Connection refused"))
        self.assertEqual(state, AcquisitionState.NETWORK_ERROR)
        self.assertEqual(classification, "connection_failed")
        self.assertIsNone(http_status)

    def test_proxy_error_is_network_error(self):
        """`ProxyError` is a `SessionError` subclass with no blocked-status
        message — crawlee's own heuristic for a transport failure that looks
        proxy-related. Still a connectivity fault, not a resource that
        doesn't exist.
        """
        error = ProxyError("All connection attempts failed")
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.NETWORK_ERROR)
        self.assertEqual(classification, "proxy_error")
        self.assertIsNone(http_status)

    def test_5xx_server_status_error_is_network_error(self):
        error = HttpStatusCodeError("Error status code returned", 503)
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.NETWORK_ERROR)
        self.assertEqual(classification, "http_503")
        self.assertEqual(http_status, 503)

    def test_other_4xx_client_status_error_is_network_error(self):
        """A 4xx that isn't 404/401/403/429 (e.g. 410 Gone) falls back to
        NETWORK_ERROR, matching the pre-Crawlee http_acquirer.py's behavior.
        """
        error = HttpClientStatusCodeError("Client error status code returned", 410)
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.NETWORK_ERROR)
        self.assertEqual(classification, "http_410")
        self.assertEqual(http_status, 410)

    def test_unmatched_session_error_is_network_error(self):
        """A `SessionError` whose message doesn't carry a parseable blocked
        status code (e.g. crawlee wording changes) must still fail safely
        as a connectivity fault, not silently misclassify as something else.
        """
        error = SessionError("Some other session-blocking condition")
        state, classification, http_status = classify_http_outcome(error)
        self.assertEqual(state, AcquisitionState.NETWORK_ERROR)
        self.assertEqual(classification, "session_blocked")
        self.assertIsNone(http_status)


if __name__ == "__main__":
    unittest.main()
