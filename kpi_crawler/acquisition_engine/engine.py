"""The Acquisition Engine: "acquire this source."

Orchestrates discovery, concurrent HTTP acquisition with adaptive
concurrency/backoff/proxy rotation, escalation to browser acquisition for
dynamic content, and records every attempt/decision/artifact through the
Evidence Ledger. Presentation (terminal output) is entirely decoupled: the
engine only calls `sink.emit(event)`.
"""

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from ..discovery import discover_links
from .adaptive import AdaptivePolicy
from .browser_acquirer import BrowserManager, dynamic_content_likely, safe_fingerprint_summary
from .concurrency import AdjustableGate
from .contract import AcquisitionState, RunSummary, derive_run_status
from .events import AttemptEvent, BackoffEvent, EventSink, FinalSummaryEvent, ImportantEvent, ProgressSnapshot
from .http_acquirer import attempt_http
from .ledger import EvidenceLedger
from .proxy import ProxyConfig, ProxyHealthState, ProxyPool
from .raw_storage import write_raw
from .sessions import SessionHealth, SessionManager
from .storage import RunStorage, safe_site_id


@dataclass
class EngineConfig:
    max_depth: int = 2
    max_artifacts: int = 50
    same_host_only: bool = True
    timeout_seconds: float = 10.0
    max_artifact_bytes: int = 50_000_000
    max_retries: int = 3
    initial_concurrency: int = 8
    min_concurrency: int = 1
    max_concurrency: int = 8
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    restore_after_successes: int = 5
    enable_browser_escalation: bool = True
    browser_headless: bool = True
    enable_fingerprinting: bool = True
    proxy_configs: list[ProxyConfig] = field(default_factory=list)
    proxy_failure_threshold: int = 3
    progress_interval_seconds: float = 5.0
    # A URL's Nth consecutive same-classification failure (0-indexed
    # retry_number) that triggers a one-time "repeated failures" important
    # event, so an operator sees it without every single retry being noisy.
    repeated_failure_threshold: int = 2
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


@dataclass
class _Stats:
    processed: int = 0
    acquired: int = 0
    failed: int = 0
    retries: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class AcquisitionEngine:
    def __init__(self, config: EngineConfig, ledger: EvidenceLedger, sink: EventSink):
        self._config = config
        self._ledger = ledger
        self._sink = sink
        self._adaptive = AdaptivePolicy(
            initial_concurrency=config.initial_concurrency,
            min_concurrency=config.min_concurrency,
            max_concurrency=config.max_concurrency,
            base_backoff_seconds=config.base_backoff_seconds,
            max_backoff_seconds=config.max_backoff_seconds,
            restore_after_successes=config.restore_after_successes,
        )
        self._proxies = ProxyPool(config.proxy_configs, config.proxy_failure_threshold)
        self._sessions = SessionManager()
        self._browser: BrowserManager | None = None
        self._browser_lock = threading.Lock()
        self._gates: dict[str, AdjustableGate] = {}
        self._gates_lock = threading.Lock()
        self._stats = _Stats()
        self._discovered: set[str] = set()
        self._discovered_lock = threading.Lock()
        self._start_time = 0.0
        self._stop_ticker = threading.Event()
        self._cancelled = threading.Event()
        self._run_storage: RunStorage | None = None

    def request_shutdown(self) -> None:
        """Cooperative cancellation: stop scheduling new waves and stop
        retrying URLs already in flight, but never abort a request mid-call
        or touch already-recorded evidence. Safe to call from any thread
        (e.g. a signal handler).
        """
        self._cancelled.set()

    def _gate_for(self, domain: str) -> AdjustableGate:
        with self._gates_lock:
            gate = self._gates.get(domain)
            if gate is None:
                gate = AdjustableGate(self._adaptive.current_concurrency(domain))
                self._gates[domain] = gate
            return gate

    def _ensure_browser(self) -> BrowserManager:
        with self._browser_lock:
            if self._browser is None:
                self._emit_important("🌐", "Starting browser", "→ launching headless Chromium for the first time")
                browser = BrowserManager(
                    headless=self._config.browser_headless,
                    enable_fingerprinting=self._config.enable_fingerprinting,
                )
                browser.start()
                self._browser = browser
            return self._browser

    def _emit_important(self, icon: str, headline: str, *detail_lines: str, data: dict | None = None) -> None:
        self._sink.emit(
            ImportantEvent(
                at=datetime.now(timezone.utc), icon=icon, headline=headline,
                detail_lines=detail_lines, data=data or {},
            )
        )

    def _note_discovered(self, urls: list[str]) -> int:
        """Record newly-found URLs in the crawl-wide discovery set (a
        superset of what ever gets attempted — it also includes URLs
        filtered out for being off-host or over budget, which the Evidence
        Ledger never sees since nothing was ever attempted for them).
        Returns how many were genuinely new.
        """
        with self._discovered_lock:
            before = len(self._discovered)
            self._discovered.update(urls)
            return len(self._discovered) - before

    def _progress_ticker(self, current_domain_ref: list[str | None]) -> None:
        self._emit_progress(current_domain_ref[0])
        while not self._stop_ticker.wait(self._config.progress_interval_seconds):
            self._emit_progress(current_domain_ref[0])

    def _emit_progress(self, current_domain: str | None) -> None:
        with self._stats.lock:
            processed, acquired, failed, retries = (
                self._stats.processed,
                self._stats.acquired,
                self._stats.failed,
                self._stats.retries,
            )
        with self._discovered_lock:
            discovered = len(self._discovered)
        elapsed = time.monotonic() - self._start_time
        rate = processed / elapsed if elapsed > 0 else 0.0
        concurrency = self._adaptive.current_concurrency(current_domain) if current_domain else self._config.initial_concurrency
        self._sink.emit(
            ProgressSnapshot(
                at=datetime.now(timezone.utc),
                elapsed_seconds=elapsed,
                current_domain=current_domain,
                discovered=discovered,
                processed=processed,
                acquired=acquired,
                partial=0,
                failed=failed,
                retries=retries,
                concurrency=concurrency,
                request_rate_per_s=rate,
                active_browser_sessions=self._browser.active_session_count() if self._browser else 0,
            )
        )

    def _emit_attempt(self, attempt_record, *, session_id: str) -> None:
        fingerprint_summary = None
        if attempt_record.acquisition_method == "browser" and self._browser is not None:
            fingerprint_summary = safe_fingerprint_summary(self._browser.fingerprint_for(session_id))
        self._sink.emit(AttemptEvent(attempt=attempt_record, fingerprint_summary=fingerprint_summary))

    def _attempt_one(self, run_id: int, url: str, discovered_from: str | None) -> tuple[AcquisitionState, bytes | None, str | None]:
        """Acquire one URL with retries/backoff/proxy/escalation, recording
        every attempt. Returns (final_state, content_if_successful, content_type).
        """
        domain = _domain(url)
        gate = self._gate_for(domain)
        session = self._sessions.acquire(domain)
        retry_number = 0
        last_state = AcquisitionState.NETWORK_ERROR
        announced_timeout_escalation = False
        announced_repeated_network_errors = False

        while retry_number <= self._config.max_retries:
            if retry_number > 0 and self._cancelled.is_set():
                return last_state, None, None

            gate.acquire()
            try:
                proxy = self._proxies.acquire()
                concurrency_now = self._adaptive.current_concurrency(domain)
                result = attempt_http(
                    url,
                    timeout_seconds=self._config.timeout_seconds,
                    max_bytes=self._config.max_artifact_bytes,
                    proxy=proxy,
                )
            finally:
                gate.release()

            if proxy is not None:
                if result.success:
                    if self._proxies.report_success(proxy.proxy_id):
                        self._emit_important("✓", f"Proxy {proxy.proxy_id} recovered", "→ back to healthy")
                        self._ledger.record_adaptive_decision(
                            run_id=run_id, domain=domain, decision_type="proxy_recovered",
                            reason="request succeeded", before_value="unhealthy", after_value="healthy",
                        )
                elif result.state != AcquisitionState.NOT_FOUND:
                    # A 404 says nothing about this proxy's health — the
                    # resource just isn't there, on any proxy. Don't let it
                    # count toward quarantine.
                    health = self._proxies.report_failure(proxy.proxy_id)
                    if health == ProxyHealthState.QUARANTINED:
                        self._emit_important("⚠", f"Proxy {proxy.proxy_id} unhealthy", "→ Proxy quarantined")
                        self._ledger.record_adaptive_decision(
                            run_id=run_id, domain=domain, decision_type="proxy_quarantined",
                            reason="failure threshold reached", before_value="healthy", after_value="quarantined",
                        )

            backoff_applied = None
            adaptive_decision_label = None
            if result.state == AcquisitionState.RATE_LIMITED:
                signal = self._adaptive.on_rate_limited(domain)
                backoff_applied = signal.backoff_seconds
                gate.set_limit(signal.after_value)
                if signal.decision_type == "concurrency_reduced":
                    adaptive_decision_label = f"concurrency {signal.before_value} -> {signal.after_value}"
                    self._emit_important(
                        "⚠", "429 detected",
                        f"→ Reducing concurrency {signal.before_value} → {signal.after_value}",
                        f"→ Backing off for {backoff_applied:.0f}s",
                    )
                    self._ledger.record_adaptive_decision(
                        run_id=run_id, domain=domain, decision_type="concurrency_reduced",
                        reason="429 detected", before_value=str(signal.before_value), after_value=str(signal.after_value),
                    )
                else:
                    self._emit_important("⚠", "429 detected", f"→ Backing off for {backoff_applied:.0f}s")
            elif result.state == AcquisitionState.TIMEOUT:
                if retry_number >= self._config.repeated_failure_threshold and not announced_timeout_escalation:
                    announced_timeout_escalation = True
                    self._emit_important("⏱", "Timeout escalation", f"→ {retry_number + 1} consecutive timeouts for {url}")
            elif result.state == AcquisitionState.NETWORK_ERROR:
                if retry_number >= self._config.repeated_failure_threshold and not announced_repeated_network_errors:
                    announced_repeated_network_errors = True
                    label = "Repeated 5xx responses" if (result.failure_classification or "").startswith("http_5") else "Repeated network errors"
                    self._emit_important("⚠", label, f"→ {retry_number + 1} consecutive failures for {url}")
            else:
                signal = self._adaptive.on_success(domain) if result.success else None
                if signal is not None:
                    gate.set_limit(signal.after_value)
                    adaptive_decision_label = f"concurrency {signal.before_value} -> {signal.after_value}"
                    self._emit_important(
                        "✓", "Healthy streak detected",
                        f"→ Gradually restoring concurrency {signal.before_value} → {signal.after_value}",
                    )
                    self._ledger.record_adaptive_decision(
                        run_id=run_id, domain=domain, decision_type="concurrency_restored",
                        reason="healthy period detected", before_value=str(signal.before_value), after_value=str(signal.after_value),
                    )

            state = result.state
            content = result.content
            content_type = result.content_type
            artifact_id = None

            if (
                result.success
                and self._config.enable_browser_escalation
                and dynamic_content_likely(result.content, result.content_type)
            ):
                self._emit_important("🌐", "Dynamic content detected", "→ Switching HTTP → Browser acquisition")
                self._ledger.record_adaptive_decision(
                    run_id=run_id, domain=domain, decision_type="method_escalated",
                    reason="dynamic content detected", before_value="http", after_value="browser",
                )
                # This HTTP fetch is not the accepted result — it will be retried via browser.
                attempt_record = self._ledger.record_attempt(
                    run_id=run_id, url=url, domain=domain, acquisition_method="http",
                    session_id=session.session_id, proxy_id=proxy.proxy_id if proxy else None,
                    proxy_status=self._proxies.health_of(proxy.proxy_id).value if proxy else None,
                    http_status=result.http_status, latency_ms=result.latency_ms, retry_number=retry_number,
                    retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
                    backoff_applied_seconds=None, concurrency_at_attempt=concurrency_now,
                    rate_limit_detected=False, failure_classification="escalated_to_browser",
                    adaptive_decision="method_escalated", final_result=AcquisitionState.PENDING_RETRY,
                    artifact_id=None,
                )
                self._emit_attempt(attempt_record, session_id=session.session_id)
                browser_result = self._ensure_browser().attempt(url, session.session_id, timeout_seconds=self._config.timeout_seconds)
                state, content, content_type = browser_result.state, browser_result.content, browser_result.content_type
                if state == AcquisitionState.SUCCESS:
                    artifact = self._store_artifact(
                        run_id, url, discovered_from, "browser", browser_result.content, browser_result.content_type,
                        browser_result.http_status, browser_result.final_url, (), session.session_id,
                    )
                    artifact_id = artifact.artifact_id
                elif browser_result.state == AcquisitionState.BROWSER_ERROR:
                    self._emit_important("⚠", "Browser acquisition failed", f"→ {url}", browser_result.error_message or "")
                attempt_record = self._ledger.record_attempt(
                    run_id=run_id, url=url, domain=domain, acquisition_method="browser",
                    session_id=session.session_id, proxy_id=None, proxy_status=None,
                    http_status=browser_result.http_status, latency_ms=browser_result.latency_ms,
                    retry_number=retry_number, retry_budget=self._config.max_retries,
                    timeout_seconds=self._config.timeout_seconds, backoff_applied_seconds=None,
                    concurrency_at_attempt=concurrency_now, rate_limit_detected=False,
                    failure_classification=browser_result.failure_classification,
                    adaptive_decision=None, final_result=state, artifact_id=artifact_id,
                    error_message=browser_result.error_message,
                )
                self._emit_attempt(attempt_record, session_id=session.session_id)
            else:
                if result.success:
                    artifact = self._store_artifact(
                        run_id, url, discovered_from, "http", result.content, result.content_type,
                        result.http_status, result.final_url, result.redirect_chain, session.session_id,
                    )
                    artifact_id = artifact.artifact_id
                attempt_record = self._ledger.record_attempt(
                    run_id=run_id, url=url, domain=domain, acquisition_method="http",
                    session_id=session.session_id, proxy_id=proxy.proxy_id if proxy else None,
                    proxy_status=self._proxies.health_of(proxy.proxy_id).value if proxy else None,
                    http_status=result.http_status, latency_ms=result.latency_ms, retry_number=retry_number,
                    retry_budget=self._config.max_retries, timeout_seconds=self._config.timeout_seconds,
                    backoff_applied_seconds=backoff_applied, concurrency_at_attempt=concurrency_now,
                    rate_limit_detected=result.state == AcquisitionState.RATE_LIMITED,
                    failure_classification=result.failure_classification, adaptive_decision=adaptive_decision_label,
                    final_result=state, artifact_id=artifact_id, error_message=result.error_message,
                )
                self._emit_attempt(attempt_record, session_id=session.session_id)

            if state == AcquisitionState.SUCCESS:
                self._sessions.report_success(session.session_id)
                return state, content, content_type

            if state != AcquisitionState.NOT_FOUND:
                # A 404 says nothing about this session's health — the
                # resource just isn't there, under any identity. Don't let
                # it count toward retirement.
                health = self._sessions.report_failure(session.session_id)
                if health == SessionHealth.UNHEALTHY:
                    old_session_id = session.session_id
                    self._sessions.retire(old_session_id, domain)
                    if self._browser is not None:
                        self._browser.retire_session(old_session_id)
                    session = self._sessions.acquire(domain)
                    self._emit_important(
                        "⚠", f"Session {old_session_id} retired", f"→ replacement session created: {session.session_id}",
                    )
                    self._ledger.record_adaptive_decision(
                        run_id=run_id, domain=domain, decision_type="session_retired",
                        reason="consecutive_failure_threshold", before_value=old_session_id,
                        after_value=session.session_id,
                    )

            last_state = state
            with self._stats.lock:
                self._stats.retries += 1
            if backoff_applied:
                self._sink.emit(
                    BackoffEvent(
                        at=datetime.now(timezone.utc), seconds=backoff_applied, reason=state.value,
                        domain=domain, concurrency=self._adaptive.current_concurrency(domain),
                    )
                )
                time.sleep(backoff_applied)
            retry_number += 1

        self._emit_important("⚠", "Retry budget exhausted", f"→ {url}", f"→ final result: {last_state.value}")
        return last_state, None, None

    def _store_artifact(
        self, run_id, source_url, discovered_from, method, content, content_type, http_status, final_url, redirect_chain, session_id,
    ):
        checksum = hashlib.sha256(content).hexdigest()
        if self._run_storage is None:
            self._run_storage = RunStorage(self._config.storage_dir, safe_site_id(source_url), run_id)
        raw_path = self._run_storage.write_raw_artifact(content, checksum, content_type, source_url)
        return self._ledger.record_artifact(
            run_id=run_id, source_url=source_url, canonical_url=None, discovered_from=discovered_from,
            fetched_at=datetime.now(timezone.utc), content_type=content_type, http_status=http_status,
            acquisition_method=method, raw_location=str(raw_path), content_size=len(content),
            checksum=checksum, encoding=None, final_url=final_url, redirect_chain=redirect_chain,
            session_id=session_id,
        )

    def run(self, root_url: str) -> tuple[RunSummary, int]:
        run_id = self._ledger.create_run(root_url)
        site_id = safe_site_id(root_url)
        self._run_storage = RunStorage(self._config.storage_dir, site_id, run_id)
        root_domain = _domain(root_url)
        self._emit_important("🚀", "Starting acquisition")
        self._emit_important("🌐", root_domain)

        self._start_time = time.monotonic()
        current_domain_ref: list[str | None] = [root_domain]
        ticker = threading.Thread(target=self._progress_ticker, args=(current_domain_ref,), daemon=True)
        ticker.start()

        visited: set[str] = set()
        final_results: dict[str, AcquisitionState] = {}
        root_parsed = urlsplit(root_url)
        seeds: list[tuple[str, int, str | None]] = [(_normalize(root_url), 0, None)]
        if root_parsed.scheme in {"http", "https"}:
            base = f"{root_parsed.scheme}://{root_parsed.netloc}"
            seeds.append((f"{base}/robots.txt", 0, root_url))
            seeds.append((f"{base}/sitemap.xml", 0, root_url))
        queue: deque[tuple[str, int, str | None]] = deque(seeds)
        self._note_discovered([url for url, _, _ in seeds])

        executor = ThreadPoolExecutor(max_workers=max(self._config.max_concurrency, 1))
        try:
            while queue and not self._cancelled.is_set() and len(visited) < self._config.max_artifacts:
                wave: list[tuple[str, int, str | None]] = []
                while queue and len(visited) + len(wave) < self._config.max_artifacts:
                    url, depth, parent = queue.popleft()
                    if url in visited or depth > self._config.max_depth:
                        continue
                    if self._config.same_host_only and _domain(url) != root_domain:
                        continue
                    visited.add(url)
                    wave.append((url, depth, parent))

                if not wave:
                    break

                current_domain_ref[0] = _domain(wave[0][0])

                futures = {executor.submit(self._attempt_one, run_id, url, parent): (url, depth) for url, depth, parent in wave}
                next_wave_links: list[tuple[str, int, str | None]] = []
                for future, (url, depth) in futures.items():
                    state, content, content_type = future.result()
                    final_results[url] = state
                    with self._stats.lock:
                        self._stats.processed += 1
                        if state == AcquisitionState.SUCCESS:
                            self._stats.acquired += 1
                        else:
                            self._stats.failed += 1

                    if state == AcquisitionState.SUCCESS and content is not None and depth < self._config.max_depth:
                        for link in discover_links(url, content, content_type or ""):
                            if link.is_resource_reference:
                                # A <link rel="stylesheet"|"icon"|...> — a
                                # page-independent resource reference, not a
                                # page to crawl. Still acquirable directly if
                                # ever given as an explicit target.
                                continue
                            next_depth = depth if link.is_pagination else depth + 1
                            next_wave_links.append((_normalize(link.url), next_depth, url))

                new_count = self._note_discovered([url for url, _, _ in next_wave_links])
                if new_count:
                    with self._discovered_lock:
                        total = len(self._discovered)
                    self._emit_important("🔎", f"Discovered {new_count} new URL(s)", f"→ {total} total", data={"total_discovered": total})

                queue.extend(next_wave_links)
        finally:
            self._stop_ticker.set()
            ticker.join(timeout=1)
            executor.shutdown(wait=True)
            if self._browser is not None:
                self._browser.stop()

        status = AcquisitionState.INTERRUPTED if self._cancelled.is_set() else derive_run_status(final_results)
        with self._discovered_lock:
            total_discovered = len(self._discovered)
        self._ledger.complete_run(run_id, status, {"discovered": total_discovered, "attempted": len(final_results)})

        icon = _STATUS_ICON.get(status, "•")
        self._emit_important(icon, f"Run finished: {status.value}")

        summary = self._ledger.summarize_run(run_id)
        summary = dataclasses.replace(summary, discovered=total_discovered)

        decisions = self._ledger.adaptive_decisions_for_run(run_id)
        adaptive_action_counts = dict(Counter(d.decision_type for d in decisions))

        artifacts = self._ledger.artifacts_for_run(run_id)
        attempts = self._ledger.attempts_for_run(run_id)
        self._run_storage.write_evidence(attempts, decisions)
        self._run_storage.write_program2_handoff(artifacts, summary)
        self._run_storage.write_manifest(summary, artifacts)
        self._run_storage.write_report(summary, artifacts, attempts, decisions)

        self._sink.emit(
            FinalSummaryEvent(
                summary=summary,
                evidence_path=str(self._run_storage.run_dir),
                adaptive_action_counts=adaptive_action_counts,
            )
        )
        return summary, run_id
