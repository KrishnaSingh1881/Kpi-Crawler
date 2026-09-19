"""Playwright-based browser acquisition, used only when HTTP acquisition is
escalated (dynamic content detected) or a caller explicitly requests it.

Playwright's sync API is confined to whichever thread calls
`sync_playwright().start()` and cannot be driven from other threads (it
raises `greenlet.error: cannot switch to a different thread`). `BrowserManager`
therefore marshals every call onto one dedicated worker thread via a
single-worker executor, so any of the engine's concurrent HTTP worker threads
can request browser acquisition without touching Playwright objects directly.

Fingerprint management: each session's `BrowserContext` is created with
`crawlee.fingerprint_suite.DefaultFingerprintGenerator` + `browserforge`'s
Playwright injector (`browserforge.injectors.playwright.NewContext`) — the
exact mechanism Crawlee's own `PlaywrightBrowserController` uses internally
(`crawlee/browsers/_playwright_browser_controller.py::_create_browser_context`,
crawlee 1.10.1). One fingerprint is generated per session at context-creation
time and reused for every page in that context (consistent within a
session); a new session gets a freshly generated, independent profile.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import re
import time
from typing import Any

from .contract import AcquisitionState

# A crude but effective SPA-shell heuristic: a small HTML body dominated by
# script tags, with very little visible text once tags are stripped, is very
# likely a client-rendered shell that plain HTTP acquisition cannot read.
_TAG_RE = re.compile(rb"<[^>]+>")
_SCRIPT_RE = re.compile(rb"<script[\s\S]*?</script>", re.IGNORECASE)
_MIN_VISIBLE_TEXT_BYTES = 200
_MIN_SCRIPT_RATIO = 0.4


def safe_fingerprint_summary(fingerprint: Any) -> str | None:
    """A short, non-reversible descriptor of a fingerprint for --verbose
    display: platform plus an 8-char digest of the user-agent string. Never
    the raw headers/UA/screen profile — none of that is a secret (it's what
    any website already sees), but a full dump is noise an operator doesn't
    need, so this is a deliberately minimal, safe summary rather than an
    unfiltered object dump.
    """
    if fingerprint is None:
        return None
    ua = fingerprint.navigator.userAgent
    platform = fingerprint.navigator.platform
    tag = hashlib.sha256(ua.encode("utf-8")).hexdigest()[:8]
    return f"{platform}:{tag}"


def dynamic_content_likely(content: bytes, content_type: str | None) -> bool:
    if not content_type or "html" not in content_type:
        return False
    if not content:
        return False
    script_bytes = sum(len(m) for m in _SCRIPT_RE.findall(content))
    visible = _TAG_RE.sub(b"", _SCRIPT_RE.sub(b"", content)).strip()
    script_ratio = script_bytes / len(content) if content else 0.0
    return len(visible) < _MIN_VISIBLE_TEXT_BYTES and script_ratio > _MIN_SCRIPT_RATIO


@dataclass(frozen=True)
class BrowserAttemptResult:
    success: bool
    content: bytes | None
    content_type: str | None
    http_status: int | None
    final_url: str | None
    latency_ms: float
    checksum: str | None
    state: AcquisitionState
    failure_classification: str | None
    error_message: str | None


class _BrowserWorker:
    """The actual Playwright driver. Every method here must only ever be
    called from the one thread that called `start()`.
    """

    def __init__(self, *, headless: bool, enable_fingerprinting: bool):
        self._headless = headless
        self._enable_fingerprinting = enable_fingerprinting
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: dict[str, Any] = {}
        self._fingerprint_generator: Any = None
        self._fingerprints: dict[str, Any] = {}

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        if self._enable_fingerprinting:
            from crawlee.fingerprint_suite import DefaultFingerprintGenerator

            self._fingerprint_generator = DefaultFingerprintGenerator()

    def stop(self) -> None:
        for context in list(self._contexts.values()):
            context.close()
        self._contexts.clear()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def active_session_count(self) -> int:
        return len(self._contexts)

    def retire_session(self, session_id: str) -> None:
        context = self._contexts.pop(session_id, None)
        self._fingerprints.pop(session_id, None)
        if context is not None:
            context.close()

    def fingerprint_for(self, session_id: str) -> Any:
        return self._fingerprints.get(session_id)

    def _context_for(self, session_id: str):
        context = self._contexts.get(session_id)
        if context is None:
            if self._fingerprint_generator is not None:
                from browserforge.injectors.playwright import NewContext

                fingerprint = self._fingerprint_generator.generate()
                context = NewContext(browser=self._browser, fingerprint=fingerprint)
                self._fingerprints[session_id] = fingerprint
            else:
                context = self._browser.new_context()
            self._contexts[session_id] = context
        return context

    def attempt(self, url: str, session_id: str, *, timeout_seconds: float) -> BrowserAttemptResult:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        context = self._context_for(session_id)
        page = context.new_page()
        started = time.monotonic()
        try:
            response = page.goto(url, timeout=timeout_seconds * 1000, wait_until="networkidle")
            content = page.content().encode("utf-8")
            latency_ms = (time.monotonic() - started) * 1000
            return BrowserAttemptResult(
                success=True,
                content=content,
                content_type="text/html",
                http_status=response.status if response is not None else None,
                final_url=page.url,
                latency_ms=latency_ms,
                checksum=hashlib.sha256(content).hexdigest(),
                state=AcquisitionState.SUCCESS,
                failure_classification=None,
                error_message=None,
            )
        except PlaywrightTimeoutError as exc:
            latency_ms = (time.monotonic() - started) * 1000
            return BrowserAttemptResult(
                success=False,
                content=None,
                content_type=None,
                http_status=None,
                final_url=None,
                latency_ms=latency_ms,
                checksum=None,
                state=AcquisitionState.TIMEOUT,
                failure_classification="browser_navigation_timeout",
                error_message=str(exc),
            )
        except PlaywrightError as exc:
            latency_ms = (time.monotonic() - started) * 1000
            return BrowserAttemptResult(
                success=False,
                content=None,
                content_type=None,
                http_status=None,
                final_url=None,
                latency_ms=latency_ms,
                checksum=None,
                state=AcquisitionState.BROWSER_ERROR,
                failure_classification="browser_error",
                error_message=str(exc),
            )
        finally:
            page.close()


class BrowserManager:
    """Thread-confined facade over `_BrowserWorker`: every public method here
    is safe to call from any thread, and internally runs on the one dedicated
    browser thread this manager owns for its whole lifetime.
    """

    def __init__(self, *, headless: bool = True, enable_fingerprinting: bool = True):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")
        self._worker = _BrowserWorker(headless=headless, enable_fingerprinting=enable_fingerprinting)

    def _run(self, fn, *args, **kwargs):
        return self._executor.submit(fn, *args, **kwargs).result()

    def start(self) -> None:
        self._run(self._worker.start)

    def stop(self) -> None:
        try:
            self._run(self._worker.stop)
        finally:
            self._executor.shutdown(wait=True)

    def active_session_count(self) -> int:
        return self._run(self._worker.active_session_count)

    def retire_session(self, session_id: str) -> None:
        self._run(self._worker.retire_session, session_id)

    def fingerprint_for(self, session_id: str) -> Any:
        """The `browserforge.fingerprints.Fingerprint` generated for this
        session's browser context, or `None` if fingerprinting is disabled or
        the session has no context yet. Plain data (no Playwright handle), so
        safe to read from any thread.
        """
        return self._run(self._worker.fingerprint_for, session_id)

    def attempt(self, url: str, session_id: str, *, timeout_seconds: float) -> BrowserAttemptResult:
        return self._run(self._worker.attempt, url, session_id, timeout_seconds=timeout_seconds)
