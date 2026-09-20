"""The Acquisition Engine: "acquire this source."

Built directly on `crawlee.crawlers.HttpCrawler` and, for pages that turn out
to need real JavaScript execution, `crawlee.crawlers.PlaywrightCrawler`.
Crawlee owns retries, session rotation, and concurrency scaling; this
module's job is narrower than the pre-Crawlee engine it replaces: seed the
crawl, map whatever Crawlee surfaces for one attempt onto our own
`AcquisitionState` contract, call `discovery.discover_links` ourselves
(Crawlee's own `enqueue_links` would bypass `is_resource_reference`
filtering), and record every attempt/artifact through the Evidence Ledger
exactly as before.

## Browser escalation

Crawlee has no single built-in "try HTTP, escalate this one URL to a browser
if it looks like a SPA shell" mode, and `HttpCrawler`/`PlaywrightCrawler` are
different classes with entirely separate context pipelines — a router/label
can only pick a handler *within* one crawler instance, it cannot mix HTTP
and browser processing in one (confirmed by reading crawlee 1.10.1's
`Router`/`ContextPipeline` source). So this engine runs **two** crawler
instances concurrently, each with its own run-scoped `RequestQueue`:

  - The HTTP crawler's handler evaluates `dynamic_content_likely` on every
    successful HTML response. If it fires, the HTTP result is *not* the
    accepted artifact — the URL is re-enqueued into the Playwright crawler's
    queue instead (`playwright_queue.add_requests(...)`), and a
    `PENDING_RETRY` attempt is recorded for the HTTP fetch, same as before.
  - The Playwright crawler's handler stores the rendered content as the
    artifact and does its *own* link discovery — but enqueues anything it
    finds back into the **HTTP** queue, not its own, since every discovered
    link starts as an HTTP attempt by default (only escalating again if
    that fetch also looks like a shell). This mirrors the old engine's
    single-BFS-queue behavior exactly, just split across two queues instead
    of one.

Both crawlers run with `keep_alive=True` (browser escalation on) so neither
one's `AutoscaledPool` gives up just because *its own* queue is momentarily
empty — a `RequestQueue.is_finished()` has no idea the other crawler might
still feed it work (verified against crawlee source: an empty, non-keep-alive
queue reports "finished" immediately). `_wait_for_both_queues_idle` is this
engine's own termination detection: only once both queues are empty *and*
nothing is mid-handler, stable across a few consecutive polls, are both
crawlers told to `.stop()`. With browser escalation off, none of this
applies: the HTTP crawler runs exactly as it did before browser support
existed (`keep_alive=False`, no second crawler, no drain loop).

Because Crawlee's crawl loop runs as cooperatively-scheduled coroutines on one
event loop thread (not a thread pool), none of the counters/sets this module
keeps for its own bookkeeping (`_discovered`, `_stats`, `_browser_in_flight`)
need a lock: nothing here ever runs two of these callbacks in true parallel,
only interleaved at `await` points.

## Blocked-status backoff (403/429)

Crawlee's own `ConcurrencySettings` is copied into `AutoscaledPool`'s private
ints at construction time and never re-read, so there is no supported way to
lower the crawl's overall concurrency ceiling at runtime in response to an
application-level signal like "this domain just 403'd/429'd us". `_on_blocked_signal`
+ `_AdjustableAsyncGate` + the HTTP crawler's own `pre_navigation_hook`
extension point together form a small, second, narrower throttle — scoped to
one domain, only active once that domain has actually signaled it wants us to
slow down — sitting *underneath* Crawlee's own concurrency ceiling rather
than replacing it. Restored after a few consecutive full-speed successes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from crawlee import ConcurrencySettings, Request
from crawlee.configuration import Configuration
from crawlee.crawlers import HttpCrawler, HttpCrawlingContext, PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.errors import HttpClientStatusCodeError, HttpStatusCodeError, ProxyError, SessionError
from crawlee.fingerprint_suite import DefaultFingerprintGenerator
from crawlee.sessions import SessionPool
from crawlee.storages import RequestQueue

from ..discovery import discover_links
from .contract import AcquisitionState, RunSummary, derive_run_status
from .events import AttemptEvent, EventSink, FinalSummaryEvent, ImportantEvent, ProgressSnapshot
from .ledger import EvidenceLedger
from .storage import RunStorage, safe_site_id


@dataclass
class EngineConfig:
    max_depth: int = 2
    max_artifacts: int = 50
    same_host_only: bool = True
    timeout_seconds: float = 10.0
    max_artifact_bytes: int = 50_000_000
    max_retries: int = 3
    max_concurrency: int = 8
    progress_interval_seconds: float = 5.0
    # A 403 (ACCESS_DENIED) and a 429 (RATE_LIMITED) are the same category of
    # signal — "this site doesn't want us going this fast" — so both trigger
    # the same response: halve this domain's HTTP concurrency and pace its
    # requests `blocked_backoff_seconds` apart, restored after
    # `blocked_restore_after_successes` consecutive full-speed successes.
    blocked_backoff_seconds: float = 1.5
    blocked_restore_after_successes: int = 5
    enable_browser_escalation: bool = True
    browser_headless: bool = True
    enable_fingerprinting: bool = True
    storage_dir: Path = Path(".data")


def _normalize(url: str) -> str:
    parts = urlsplit(url)
    return parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), fragment="").geturl()


def _domain(url: str) -> str:
    return urlsplit(url).netloc.lower()


_STATUS_ICON = {
    AcquisitionState.SUCCESS: "✓",
    AcquisitionState.PARTIAL: "⚠",
    AcquisitionState.FAILED: "✗",
    AcquisitionState.INTERRUPTED: "⏸",
}

# A crude but effective SPA-shell heuristic, ported as-is from the pre-Crawlee
# browser acquirer: a small HTML body dominated by script tags, with very
# little visible text once tags are stripped, is very likely a client-rendered
# shell that plain HTTP acquisition cannot read. Pure function, no crawlee
# dependency — portable unchanged.
_TAG_RE = re.compile(rb"<[^>]+>")
_SCRIPT_RE = re.compile(rb"<script[\s\S]*?</script>", re.IGNORECASE)
_MIN_VISIBLE_TEXT_BYTES = 200
_MIN_SCRIPT_RATIO = 0.4


def dynamic_content_likely(content: bytes, content_type: str | None) -> bool:
    if not content_type or "html" not in content_type:
        return False
    if not content:
        return False
    script_bytes = sum(len(m) for m in _SCRIPT_RE.findall(content))
    visible = _TAG_RE.sub(b"", _SCRIPT_RE.sub(b"", content)).strip()
    script_ratio = script_bytes / len(content) if content else 0.0
    return len(visible) < _MIN_VISIBLE_TEXT_BYTES and script_ratio > _MIN_SCRIPT_RATIO


# Crawlee raises a `SessionError` for a "blocked" status code (401/403/429 by
# default — see `Session._DEFAULT_BLOCKED_STATUS_CODES`) *before* the generic
# 4xx/5xx status-code check, and that exception carries no `.status_code`
# attribute of its own — only this message
# (`_raise_for_session_blocked_status_code`, crawlee 1.10.1). Parsing it back
# out is the only way to tell a 429 from a 401/403 at this point; verified
# against crawlee's actual source in Stage 0, not guessed.
_BLOCKED_STATUS_RE = re.compile(r"status code (\d+)")


def classify_http_outcome(error: Exception | None) -> tuple[AcquisitionState, str | None, int | None]:
    """Pure mapping from whatever crawlee.HttpCrawler surfaces for one
    acquisition attempt to our own contract: (AcquisitionState,
    failure_classification, http_status).

    `error` is `None` for a successful (2xx/3xx) response — the only case
    that reaches the normal request handler at all, since Crawlee's own
    status-code check raises before the handler ever sees a 4xx/5xx
    response (verified in Stage 0). Every other case here is something the
    engine's `failed_request_handler` receives instead.

    Kept as a small, pure, directly-testable function precisely because this
    mapping is the highest-risk part of the Crawlee port: getting a 404
    confused with a genuine connectivity failure would silently corrupt the
    evidence ledger's failure classification.
    """
    if error is None:
        return AcquisitionState.SUCCESS, None, None

    if isinstance(error, SessionError):
        match = _BLOCKED_STATUS_RE.search(str(error))
        if match:
            status = int(match.group(1))
            if status == 429:
                return AcquisitionState.RATE_LIMITED, f"http_{status}", status
            if status in (401, 403):
                return AcquisitionState.ACCESS_DENIED, f"http_{status}", status
            return AcquisitionState.NETWORK_ERROR, f"http_{status}", status
        # A `ProxyError` (transport failure crawlee's own heuristic decided
        # looks proxy-related — see `_httpx.py::_is_proxy_error` — this can
        # fire even with no proxy configured, purely off the error message)
        # or any other session-blocking condition. Either way: a genuine
        # connectivity/session problem, not a resource that doesn't exist.
        classification = "proxy_error" if isinstance(error, ProxyError) else "session_blocked"
        return AcquisitionState.NETWORK_ERROR, classification, None

    if isinstance(error, HttpClientStatusCodeError):
        status = error.status_code
        if status == 404:
            # A clean "not here", not a connectivity problem — kept out of
            # NETWORK_ERROR so it's never confused with a real network fault.
            return AcquisitionState.NOT_FOUND, f"http_{status}", status
        if status in (401, 403):
            return AcquisitionState.ACCESS_DENIED, f"http_{status}", status
        if status == 429:
            return AcquisitionState.RATE_LIMITED, f"http_{status}", status
        return AcquisitionState.NETWORK_ERROR, f"http_{status}", status

    if isinstance(error, HttpStatusCodeError):
        # 5xx (or an operator-configured `additional_http_error_status_codes`).
        status = error.status_code
        return AcquisitionState.NETWORK_ERROR, f"http_{status}", status

    if isinstance(error, asyncio.TimeoutError):
        return AcquisitionState.TIMEOUT, "timeout", None

    # Any other exception is a raw transport failure (DNS resolution, refused
    # connection, reset, ...) that crawlee's httpx client didn't recognize as
    # a proxy issue — a genuine network fault.
    return AcquisitionState.NETWORK_ERROR, "connection_failed", None


def classify_browser_outcome(error: Exception | None) -> tuple[AcquisitionState, str | None, int | None]:
    """Like `classify_http_outcome`, but recognizes a raw Playwright
    automation failure (a crashed page, a broken navigation, ...) as
    `BROWSER_ERROR` rather than the generic `NETWORK_ERROR` catch-all.

    `PlaywrightCrawler` shares the exact same status-code/timeout/session-
    blocking exception shapes as `HttpCrawler` (both build on the same
    `BasicCrawler._handle_status_code_response`, confirmed by reading
    crawlee 1.10.1's `_playwright_crawler.py`), so every other case
    delegates straight through to `classify_http_outcome`.
    """
    state, classification, http_status = classify_http_outcome(error)
    if classification == "connection_failed":
        from playwright.async_api import Error as PlaywrightError

        if isinstance(error, PlaywrightError):
            return AcquisitionState.BROWSER_ERROR, "browser_error", None
    return state, classification, http_status


def _content_type(content: bytes, header_value: str | None) -> str:
    """Base MIME type (no `; charset=...`), with a PDF-magic-bytes fallback
    for a server that mislabels its own response — same heuristic the
    pre-Crawlee `http_acquirer.py` used.
    """
    base = (header_value or "").split(";")[0].strip().lower()
    if base and base != "application/octet-stream":
        return base
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


@dataclass
class _Stats:
    processed: int = 0
    acquired: int = 0
    failed: int = 0
    retries: int = 0


class _AdjustableAsyncGate:
    """A concurrency gate whose limit can be lowered while requests are
    already waiting on it — `asyncio.Semaphore` only supports releasing
    more permits, not shrinking one already in use, so the blocked-status
    backoff below needs this instead.

    Deliberately narrow: this does not replace or wrap Crawlee's own
    `ConcurrencySettings`/`AutoscaledPool`, which still owns the general
    concurrency ceiling for the whole crawl. Crawlee has no supported way to
    lower that ceiling at runtime in response to an application-level signal
    like a 403/429 (`ConcurrencySettings` is copied into private ints at
    `AutoscaledPool.__init__` and never re-read) — this gate is a second,
    narrower throttle that only activates for one domain, only once that
    domain has actually signaled it wants us to slow down.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._count = 0
        self._condition = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._condition:
            while self._count >= self._limit:
                await self._condition.wait()
            self._count += 1

    async def release(self) -> None:
        async with self._condition:
            self._count -= 1
            self._condition.notify()

    async def set_limit(self, new_limit: int) -> None:
        async with self._condition:
            self._limit = new_limit
            self._condition.notify_all()


@dataclass
class _DomainSlowdown:
    """Tracks one domain's blocked-status backoff: a reduced-concurrency
    gate (see `_AdjustableAsyncGate`) plus a fixed per-request pacing delay,
    both applied only to HTTP requests for this one domain via a
    `pre_navigation_hook`. Restored (this record dropped) after enough
    consecutive full successes, mirroring the pre-Crawlee `AdaptivePolicy`'s
    `restore_after_successes` default.
    """

    gate: _AdjustableAsyncGate
    reduced_concurrency: int
    consecutive_successes: int = 0


class AcquisitionEngine:
    def __init__(self, config: EngineConfig, ledger: EvidenceLedger, sink: EventSink):
        self._config = config
        self._ledger = ledger
        self._sink = sink
        self._stats = _Stats()
        self._discovered: set[str] = set()
        self._root_domain = ""
        self._run_storage: RunStorage | None = None
        self._crawler: HttpCrawler | None = None
        self._playwright_crawler: PlaywrightCrawler | None = None
        self._shutdown_requested = False
        self._browser_in_flight = 0
        self._browser_concurrency = 1
        self._slowdowns: dict[str, _DomainSlowdown] = {}

    def request_shutdown(self) -> None:
        """Cooperative cancellation: stop scheduling new requests and let
        whatever is already in flight finish, never abort a request mid-call
        or touch already-recorded evidence. Delegates entirely to crawlee's
        own `Crawler.stop()` (verified to have exactly this semantics:
        `__is_task_ready_function` starts returning `False` immediately,
        `__is_finished_function` returns `True` only once already-running
        tasks complete, independent of `keep_alive`) rather than
        reimplementing it. Stops both crawlers when browser escalation is on.

        Safe to call before `run()` starts (e.g. a signal handler firing
        before either crawler object exists yet): the flag is applied to
        each crawler as soon as it's created.
        """
        self._shutdown_requested = True
        if self._crawler is not None:
            self._crawler.stop(reason="operator requested shutdown")
        if self._playwright_crawler is not None:
            self._playwright_crawler.stop(reason="operator requested shutdown")

    def _emit_important(self, icon: str, headline: str, *detail_lines: str, data: dict | None = None) -> None:
        self._sink.emit(
            ImportantEvent(
                at=datetime.now(timezone.utc), icon=icon, headline=headline,
                detail_lines=detail_lines, data=data or {},
            )
        )

    def _emit_attempt(self, attempt_record) -> None:
        self._sink.emit(AttemptEvent(attempt=attempt_record))

    def _note_discovered(self, urls: list[str]) -> int:
        before = len(self._discovered)
        self._discovered.update(urls)
        return len(self._discovered) - before

    async def _progress_ticker(self, start_time: float) -> None:
        while True:
            await asyncio.sleep(self._config.progress_interval_seconds)
            elapsed = time.monotonic() - start_time
            rate = self._stats.processed / elapsed if elapsed > 0 else 0.0
            self._sink.emit(
                ProgressSnapshot(
                    at=datetime.now(timezone.utc),
                    elapsed_seconds=elapsed,
                    current_domain=self._root_domain,
                    discovered=len(self._discovered),
                    processed=self._stats.processed,
                    acquired=self._stats.acquired,
                    partial=0,
                    failed=self._stats.failed,
                    retries=self._stats.retries,
                    concurrency=self._config.max_concurrency,
                    request_rate_per_s=rate,
                    active_browser_sessions=self._browser_in_flight,
                )
            )

    def _build_seed_requests(self, root_url: str) -> list[Request]:
        seeds = [Request.from_url(_normalize(root_url), user_data={"depth": 0, "discovered_from": None})]
        parsed = urlsplit(root_url)
        if parsed.scheme in {"http", "https"}:
            base = f"{parsed.scheme}://{parsed.netloc}"
            seeds.append(Request.from_url(f"{base}/robots.txt", user_data={"depth": 0, "discovered_from": root_url}))
            seeds.append(Request.from_url(f"{base}/sitemap.xml", user_data={"depth": 0, "discovered_from": root_url}))
        return seeds

    def _discover_new_links(self, url: str, content: bytes, content_type: str, depth: int) -> list[Request]:
        candidates = discover_links(url, content, content_type)
        discovered_urls = [link.url for link in candidates if not link.is_resource_reference]
        self._note_discovered(discovered_urls)

        to_enqueue: list[Request] = []
        for link in candidates:
            if link.is_resource_reference:
                continue
            if self._config.same_host_only and _domain(link.url) != self._root_domain:
                continue
            next_depth = depth if link.is_pagination else depth + 1
            to_enqueue.append(Request.from_url(link.url, user_data={"depth": next_depth, "discovered_from": url}))
        return to_enqueue

    async def _on_blocked_signal(self, run_id: int, domain: str) -> tuple[bool, int]:
        """A 403 or 429 was just observed for `domain`: halve its HTTP
        concurrency (floored at 1) and start pacing its requests
        `blocked_backoff_seconds` apart, via the gate `_http_pre_navigation_hook`
        checks. A repeat signal while already reduced just resets the
        restore countdown — it does not reduce further (matching the
        pre-Crawlee `AdaptivePolicy`, which only ever halved once per
        detection, not compounding on every subsequent one).

        Returns `(triggered_new_reduction, reduced_concurrency)` so the
        caller can label the *triggering* attempt's own `adaptive_decision`
        field, the same way the pre-Crawlee engine did.
        """
        slowdown = self._slowdowns.get(domain)
        if slowdown is not None:
            slowdown.consecutive_successes = 0
            return False, slowdown.reduced_concurrency

        reduced = max(1, self._config.max_concurrency // 2)
        self._slowdowns[domain] = _DomainSlowdown(gate=_AdjustableAsyncGate(reduced), reduced_concurrency=reduced)
        self._emit_important(
            "⚠", "403/429 detected",
            f"→ Reducing concurrency {self._config.max_concurrency} → {reduced} for {domain}",
            f"→ Pacing requests {self._config.blocked_backoff_seconds:.1f}s apart",
        )
        self._ledger.record_adaptive_decision(
            run_id=run_id, domain=domain, decision_type="concurrency_reduced",
            reason="403/429 detected", before_value=str(self._config.max_concurrency), after_value=str(reduced),
        )
        return True, reduced

    async def _note_success_for_slowdown_restore(self, run_id: int, domain: str) -> None:
        slowdown = self._slowdowns.get(domain)
        if slowdown is None:
            return
        slowdown.consecutive_successes += 1
        if slowdown.consecutive_successes < self._config.blocked_restore_after_successes:
            return
        del self._slowdowns[domain]
        self._emit_important("✓", "Healthy streak detected", f"→ Restoring full concurrency for {domain}")
        self._ledger.record_adaptive_decision(
            run_id=run_id, domain=domain, decision_type="concurrency_restored",
            reason="healthy period detected",
            before_value=str(slowdown.reduced_concurrency), after_value=str(self._config.max_concurrency),
        )

    async def _http_pre_navigation_hook(self, context) -> None:
        """Enforces `_on_blocked_signal`'s reduced concurrency + pacing for
        one domain. A no-op for every domain that hasn't triggered it — the
        common case pays only a dict lookup, not a gate/sleep.
        """
        slowdown = self._slowdowns.get(_domain(context.request.url))
        if slowdown is None:
            return
        await slowdown.gate.acquire()
        try:
            await asyncio.sleep(self._config.blocked_backoff_seconds)
        finally:
            await slowdown.gate.release()

    def _store_artifact(self, run_id, source_url, discovered_from, content, content_type, http_status, final_url, session_id, method):
        checksum = hashlib.sha256(content).hexdigest()
        raw_path = self._run_storage.write_raw_artifact(content, checksum, content_type, source_url)
        return self._ledger.record_artifact(
            run_id=run_id, source_url=source_url, canonical_url=None, discovered_from=discovered_from,
            fetched_at=datetime.now(timezone.utc), content_type=content_type, http_status=http_status,
            acquisition_method=method, raw_location=str(raw_path), content_size=len(content),
            checksum=checksum, encoding=None, final_url=final_url, redirect_chain=(),
            session_id=session_id,
        )

    async def _handle_http_success(self, context: HttpCrawlingContext, run_id: int, playwright_queue: RequestQueue | None) -> None:
        request = context.request
        url = request.url
        domain = _domain(url)
        depth = request.user_data.get("depth", 0)
        discovered_from = request.user_data.get("discovered_from")
        session_id = context.session.id if context.session else None
        retry_count = request.retry_count
        if retry_count > 0:
            self._stats.retries += 1

        content = await context.http_response.read()
        header_content_type = context.http_response.headers.get("content-type")
        content_type = _content_type(content, header_content_type)
        http_status = context.http_response.status_code
        final_url = request.loaded_url or url

        # Captured before the restore check below (which may delete this
        # domain's slowdown record) so this attempt's own row reflects
        # whatever actually applied *to it*, not the state after restoring.
        slowdown = self._slowdowns.get(domain)
        backoff_applied = self._config.blocked_backoff_seconds if slowdown else None
        concurrency_now = slowdown.reduced_concurrency if slowdown else self._config.max_concurrency
        await self._note_success_for_slowdown_restore(run_id, domain)

        if len(content) > self._config.max_artifact_bytes:
            attempt = self._ledger.record_attempt(
                run_id=run_id, url=url, domain=domain, acquisition_method="http",
                session_id=session_id, proxy_id=None, proxy_status=None,
                http_status=http_status, latency_ms=None, retry_number=retry_count,
                retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
                backoff_applied_seconds=backoff_applied, concurrency_at_attempt=concurrency_now,
                rate_limit_detected=False, failure_classification="artifact_too_large",
                adaptive_decision=None, final_result=AcquisitionState.BLOCKED, artifact_id=None,
                error_message=f"content exceeds {self._config.max_artifact_bytes}-byte limit",
            )
            self._emit_attempt(attempt)
            self._stats.processed += 1
            self._stats.failed += 1
            return

        if playwright_queue is not None and dynamic_content_likely(content, content_type):
            self._emit_important("🌐", "Dynamic content detected", "→ Switching HTTP → Browser acquisition")
            self._ledger.record_adaptive_decision(
                run_id=run_id, domain=domain, decision_type="method_escalated",
                reason="dynamic content detected", before_value="http", after_value="browser",
            )
            # This HTTP fetch is not the accepted result — it will be retried via browser.
            attempt = self._ledger.record_attempt(
                run_id=run_id, url=url, domain=domain, acquisition_method="http",
                session_id=session_id, proxy_id=None, proxy_status=None,
                http_status=http_status, latency_ms=None, retry_number=retry_count,
                retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
                backoff_applied_seconds=backoff_applied, concurrency_at_attempt=concurrency_now,
                rate_limit_detected=False, failure_classification="escalated_to_browser",
                adaptive_decision="method_escalated", final_result=AcquisitionState.PENDING_RETRY,
                artifact_id=None,
            )
            self._emit_attempt(attempt)
            await playwright_queue.add_requests(
                [Request.from_url(url, user_data={"depth": depth, "discovered_from": discovered_from})]
            )
            return

        artifact = self._store_artifact(run_id, url, discovered_from, content, content_type, http_status, final_url, session_id, method="http")
        attempt = self._ledger.record_attempt(
            run_id=run_id, url=url, domain=domain, acquisition_method="http",
            session_id=session_id, proxy_id=None, proxy_status=None,
            http_status=http_status, latency_ms=None, retry_number=retry_count,
            retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
            backoff_applied_seconds=backoff_applied, concurrency_at_attempt=concurrency_now,
            rate_limit_detected=False, failure_classification=None, adaptive_decision=None,
            final_result=AcquisitionState.SUCCESS, artifact_id=artifact.artifact_id,
        )
        self._emit_attempt(attempt)
        self._stats.processed += 1
        self._stats.acquired += 1

        if depth < self._config.max_depth:
            to_enqueue = self._discover_new_links(url, content, content_type, depth)
            if to_enqueue:
                await context.add_requests(to_enqueue)

    async def _handle_http_failure(self, context, error: Exception, run_id: int) -> None:
        request = context.request
        url = request.url
        domain = _domain(url)
        session_id = context.session.id if context.session else None
        retry_count = request.retry_count
        if retry_count > 0:
            self._stats.retries += 1

        state, classification, http_status = classify_http_outcome(error)

        # Captured before `_on_blocked_signal` (which may create this
        # domain's slowdown record) so this attempt's own row reflects
        # whatever actually applied *to it* — it triggered the reduction,
        # it didn't experience it.
        slowdown = self._slowdowns.get(domain)
        backoff_applied = self._config.blocked_backoff_seconds if slowdown else None
        concurrency_now = slowdown.reduced_concurrency if slowdown else self._config.max_concurrency

        adaptive_decision = None
        if state in (AcquisitionState.RATE_LIMITED, AcquisitionState.ACCESS_DENIED):
            triggered, reduced = await self._on_blocked_signal(run_id, domain)
            if triggered:
                adaptive_decision = f"concurrency {self._config.max_concurrency} -> {reduced}"

        attempt = self._ledger.record_attempt(
            run_id=run_id, url=url, domain=domain, acquisition_method="http",
            session_id=session_id, proxy_id=None, proxy_status=None,
            http_status=http_status, latency_ms=None, retry_number=retry_count,
            retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
            backoff_applied_seconds=backoff_applied, concurrency_at_attempt=concurrency_now,
            rate_limit_detected=state == AcquisitionState.RATE_LIMITED,
            failure_classification=classification, adaptive_decision=adaptive_decision,
            final_result=state, artifact_id=None, error_message=str(error),
        )
        self._emit_attempt(attempt)
        self._stats.processed += 1
        self._stats.failed += 1

    async def _handle_browser_success(self, context: PlaywrightCrawlingContext, run_id: int, http_queue: RequestQueue) -> None:
        request = context.request
        url = request.url
        domain = _domain(url)
        depth = request.user_data.get("depth", 0)
        discovered_from = request.user_data.get("discovered_from")
        session_id = context.session.id if context.session else None
        retry_count = request.retry_count
        if retry_count > 0:
            self._stats.retries += 1

        html = await context.page.content()
        content = html.encode("utf-8")
        content_type = "text/html"
        http_status = context.response.status if context.response is not None else None
        final_url = context.page.url or url

        if len(content) > self._config.max_artifact_bytes:
            attempt = self._ledger.record_attempt(
                run_id=run_id, url=url, domain=domain, acquisition_method="browser",
                session_id=session_id, proxy_id=None, proxy_status=None,
                http_status=http_status, latency_ms=None, retry_number=retry_count,
                retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
                backoff_applied_seconds=None, concurrency_at_attempt=self._browser_concurrency,
                rate_limit_detected=False, failure_classification="artifact_too_large",
                adaptive_decision=None, final_result=AcquisitionState.BLOCKED, artifact_id=None,
                error_message=f"content exceeds {self._config.max_artifact_bytes}-byte limit",
            )
            self._emit_attempt(attempt)
            self._stats.processed += 1
            self._stats.failed += 1
            return

        artifact = self._store_artifact(run_id, url, discovered_from, content, content_type, http_status, final_url, session_id, method="browser")
        attempt = self._ledger.record_attempt(
            run_id=run_id, url=url, domain=domain, acquisition_method="browser",
            session_id=session_id, proxy_id=None, proxy_status=None,
            http_status=http_status, latency_ms=None, retry_number=retry_count,
            retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
            backoff_applied_seconds=None, concurrency_at_attempt=self._browser_concurrency,
            rate_limit_detected=False, failure_classification=None, adaptive_decision=None,
            final_result=AcquisitionState.SUCCESS, artifact_id=artifact.artifact_id,
        )
        self._emit_attempt(attempt)
        self._stats.processed += 1
        self._stats.acquired += 1

        if depth < self._config.max_depth:
            # Every discovered link starts as an HTTP attempt by default —
            # only escalating again itself if that fetch also looks like a
            # shell — so newly found links go back into the HTTP queue, not
            # this crawler's own.
            to_enqueue = self._discover_new_links(url, content, content_type, depth)
            if to_enqueue:
                await http_queue.add_requests(to_enqueue)

    async def _handle_browser_failure(self, context, error: Exception, run_id: int) -> None:
        request = context.request
        url = request.url
        domain = _domain(url)
        session_id = context.session.id if context.session else None
        retry_count = request.retry_count
        if retry_count > 0:
            self._stats.retries += 1

        state, classification, http_status = classify_browser_outcome(error)
        attempt = self._ledger.record_attempt(
            run_id=run_id, url=url, domain=domain, acquisition_method="browser",
            session_id=session_id, proxy_id=None, proxy_status=None,
            http_status=http_status, latency_ms=None, retry_number=retry_count,
            retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
            backoff_applied_seconds=None, concurrency_at_attempt=self._browser_concurrency,
            rate_limit_detected=state == AcquisitionState.RATE_LIMITED,
            failure_classification=classification, adaptive_decision=None,
            final_result=state, artifact_id=None, error_message=str(error),
        )
        self._emit_attempt(attempt)
        self._stats.processed += 1
        self._stats.failed += 1

    async def _wait_for_both_queues_idle(
        self, http_task: asyncio.Task, playwright_task: asyncio.Task,
        http_queue: RequestQueue, playwright_queue: RequestQueue,
        *, poll_interval: float = 0.05, stable_polls_required: int = 3,
    ) -> None:
        """Termination detection for the two-crawler pipeline: each queue's
        own `is_finished()` only knows about *its own* pending/in-progress
        requests, not that the *other* crawler might still feed it work —
        so this engine has to decide for itself when there is truly nothing
        left on either side, stable across a few consecutive polls.

        Deliberately `is_finished()`, not `is_empty()`: the memory storage
        client marks a request "in progress" the instant it's dequeued
        (`_request_queue_client.py::fetch_next_request`), before the handler
        coroutine is even scheduled to run — `is_empty()` alone would report
        `True` during that window even though a handler is about to execute
        (and, e.g., about to call `add_requests` on the *other* queue),
        which caused exactly this false-idle race in testing: a URL escalated
        to the browser crawler getting stopped mid-flight because the HTTP
        queue looked momentarily quiet on the other side.

        Also returns as soon as *either* crawler's own `run()` task finishes
        on its own — e.g. `max_requests_per_crawl` reached internally calls
        `self.stop()` on that crawler alone, which can leave hundreds of
        already-discovered-but-never-to-be-dequeued links sitting in its
        queue forever. Without this check, `queue.is_finished()` (which
        reflects "empty AND nothing in progress" for *that* queue) would
        never become `True` — nothing is dequeuing those leftover pending
        items any more — and this loop would wait forever. Caught by testing
        this exact scenario against a real site with `max_artifacts` set
        below its actual link count.
        """
        stable = 0
        while stable < stable_polls_required:
            if self._shutdown_requested or http_task.done() or playwright_task.done():
                return
            await asyncio.sleep(poll_interval)
            http_idle = await http_queue.is_finished()
            playwright_idle = await playwright_queue.is_finished()
            if http_idle and playwright_idle:
                stable += 1
            else:
                stable = 0

    async def run(self, root_url: str) -> tuple[RunSummary, int]:
        run_id = self._ledger.create_run(root_url)
        site_id = safe_site_id(root_url)
        self._run_storage = RunStorage(self._config.storage_dir, site_id, run_id)
        self._root_domain = _domain(root_url)
        self._emit_important("🚀", "Starting acquisition")
        self._emit_important("🌐", self._root_domain)

        # Crawlee's own bookkeeping (request queue state, crawler statistics)
        # defaults to `./storage` relative to wherever the process happens to
        # run. Keep it out of the operator's working directory, and out of
        # the run's own output tree (which is handed off to Program 2 as-is).
        crawlee_configuration = Configuration(storage_dir=str(self._config.storage_dir / ".crawlee" / str(run_id)))

        browser_enabled = self._config.enable_browser_escalation
        self._browser_concurrency = max(1, min(4, self._config.max_concurrency))

        # An `alias`-scoped request queue is run-local (unnamed storage), not
        # the process-wide default: without this, a second `run()` call in
        # the same process would share request dedup state with the first
        # and silently skip URLs it already saw on an earlier run.
        http_queue = await RequestQueue.open(alias=f"acq-run-{run_id}-http", configuration=crawlee_configuration)
        http_crawler = HttpCrawler(
            configuration=crawlee_configuration,
            request_manager=http_queue,
            session_pool=SessionPool(),
            max_request_retries=self._config.max_retries,
            max_requests_per_crawl=self._config.max_artifacts,
            concurrency_settings=ConcurrencySettings(
                max_concurrency=max(1, self._config.max_concurrency),
                desired_concurrency=min(10, max(1, self._config.max_concurrency)),
            ),
            navigation_timeout=timedelta(seconds=self._config.timeout_seconds),
            # With browser escalation on, this crawler must not give up just
            # because its own queue is momentarily empty — the Playwright
            # crawler may still feed links back into it (see module docstring).
            keep_alive=browser_enabled,
        )
        self._crawler = http_crawler
        http_crawler.pre_navigation_hook(self._http_pre_navigation_hook)

        playwright_crawler: PlaywrightCrawler | None = None
        playwright_queue: RequestQueue | None = None
        if browser_enabled:
            playwright_queue = await RequestQueue.open(alias=f"acq-run-{run_id}-browser", configuration=crawlee_configuration)
            playwright_crawler = PlaywrightCrawler(
                configuration=crawlee_configuration,
                request_manager=playwright_queue,
                session_pool=SessionPool(),
                headless=self._config.browser_headless,
                fingerprint_generator=DefaultFingerprintGenerator() if self._config.enable_fingerprinting else None,
                max_request_retries=self._config.max_retries,
                max_requests_per_crawl=self._config.max_artifacts,
                concurrency_settings=ConcurrencySettings(
                    max_concurrency=self._browser_concurrency,
                    desired_concurrency=self._browser_concurrency,
                ),
                navigation_timeout=timedelta(seconds=self._config.timeout_seconds),
                keep_alive=True,
            )
            self._playwright_crawler = playwright_crawler

        if self._shutdown_requested:
            http_crawler.stop(reason="operator requested shutdown before the run started")
            if playwright_crawler is not None:
                playwright_crawler.stop(reason="operator requested shutdown before the run started")

        @http_crawler.router.default_handler
        async def request_handler(context: HttpCrawlingContext) -> None:
            await self._handle_http_success(context, run_id, playwright_queue)

        @http_crawler.failed_request_handler
        async def failed_request_handler(context, error: Exception) -> None:
            await self._handle_http_failure(context, error, run_id)

        @http_crawler.error_handler
        async def error_handler(context, error: Exception) -> None:
            # Fires on *every* retry attempt — including a session-rotation
            # retry for a blocked status — before crawlee decides whether to
            # retry or give up. This is the right place to react to a
            # 403/429: crawlee's own session rotation often gets a later
            # attempt through on a fresh session, so a request that
            # eventually succeeds never reaches `failed_request_handler` at
            # all — relying on that alone would silently miss most
            # real-world blocked-status occurrences, only ever catching the
            # rare case where every rotation is also blocked.
            state, _, _ = classify_http_outcome(error)
            if state in (AcquisitionState.RATE_LIMITED, AcquisitionState.ACCESS_DENIED):
                await self._on_blocked_signal(run_id, _domain(context.request.url))

        if playwright_crawler is not None:

            @playwright_crawler.router.default_handler
            async def browser_request_handler(context: PlaywrightCrawlingContext) -> None:
                self._browser_in_flight += 1
                try:
                    await self._handle_browser_success(context, run_id, http_queue)
                finally:
                    self._browser_in_flight -= 1

            @playwright_crawler.failed_request_handler
            async def browser_failed_request_handler(context, error: Exception) -> None:
                self._browser_in_flight += 1
                try:
                    await self._handle_browser_failure(context, error, run_id)
                finally:
                    self._browser_in_flight -= 1

        seeds = self._build_seed_requests(root_url)
        self._note_discovered([r.url for r in seeds])

        start_time = time.monotonic()
        progress_task = asyncio.create_task(self._progress_ticker(start_time))
        try:
            if playwright_crawler is None:
                await http_crawler.run(seeds)
            else:
                http_task = asyncio.create_task(http_crawler.run(seeds))
                playwright_task = asyncio.create_task(playwright_crawler.run())
                await self._wait_for_both_queues_idle(http_task, playwright_task, http_queue, playwright_queue)
                http_crawler.stop(reason="both HTTP and browser queues are idle")
                playwright_crawler.stop(reason="both HTTP and browser queues are idle")
                await asyncio.gather(http_task, playwright_task)
        finally:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
            await http_queue.drop()
            if playwright_queue is not None:
                await playwright_queue.drop()

        attempts = self._ledger.attempts_for_run(run_id)
        last_by_url: dict[str, AcquisitionState] = {}
        for a in attempts:
            last_by_url[a.url] = a.final_result

        status = AcquisitionState.INTERRUPTED if self._shutdown_requested else derive_run_status(last_by_url)
        self._ledger.complete_run(run_id, status, {"discovered": len(self._discovered), "attempted": len(last_by_url)})

        icon = _STATUS_ICON.get(status, "•")
        self._emit_important(icon, f"Run finished: {status.value}")

        summary = self._ledger.summarize_run(run_id)
        summary = dataclasses.replace(summary, discovered=len(self._discovered))

        artifacts = self._ledger.artifacts_for_run(run_id)
        decisions = self._ledger.adaptive_decisions_for_run(run_id)
        adaptive_action_counts: dict[str, int] = {}
        for d in decisions:
            adaptive_action_counts[d.decision_type] = adaptive_action_counts.get(d.decision_type, 0) + 1
        self._run_storage.write_evidence(attempts, decisions)
        self._run_storage.write_program2_handoff(artifacts, summary)
        self._run_storage.write_manifest(summary, artifacts)
        self._run_storage.write_report(summary, artifacts, attempts, decisions)

        self._sink.emit(
            FinalSummaryEvent(
                summary=summary, evidence_path=str(self._run_storage.run_dir),
                adaptive_action_counts=adaptive_action_counts,
            )
        )
        return summary, run_id
