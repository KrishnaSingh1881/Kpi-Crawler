"""Generalized web discovery and navigation: broad, bounded, breadth-first acquisition.

Generic web mechanics only: link-following, nested pages, downloadable
resources, sitemap/robots.txt discovery, and standards-based pagination
(`rel="next"`). No relevance scoring, semantic classification, filename-based
filtering, or priority tiers — every discovered, in-bounds URL is acquired.

One `acquisition_runs` row covers the whole crawl. Every fetch (root, sitemap,
robots.txt, or a discovered link/resource) is one `_process_source` call, so it
gets the same acquisition/extraction/storage-failure handling, provenance, and
content-addressed deduplication as the single-URL pipeline. Lineage is the
existing `acquisition_events.parent_event_id` chain: a discovered URL's event
points at the acquisition event of the page it was found on.
"""

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .acquisition import DEFAULT_MAX_ARTIFACT_BYTES, DEFAULT_RETRIES, DEFAULT_TIMEOUT_SECONDS
from .db import connection
from .discovery import discover_links
from .models import Artifact, EvidenceProvenance
from .pipeline import DEFAULT_ALLOWED_CONTENT_TYPES, FetchStatus, _process_source, _record_failure
from .storage import AcquisitionRepository


DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_ARTIFACTS = 50


@dataclass(frozen=True)
class CrawlPageResult:
    source_url: str
    depth: int
    status: FetchStatus
    artifact: Artifact | None
    evidence: tuple[EvidenceProvenance, ...]


@dataclass(frozen=True)
class CrawlResult:
    run_id: int
    discovered: int
    acquired: int
    rejected: int
    pages: tuple[CrawlPageResult, ...]


@dataclass(frozen=True)
class _QueueItem:
    url: str
    depth: int
    parent_event_id: int | None


def _normalize(url: str) -> str:
    # Scheme and host are case-insensitive per RFC 3986 6.2.2.1; the path and
    # query are not, so only scheme/host are folded here.
    scheme, netloc, path, query, _fragment = urlsplit(url)
    return f"{scheme.lower()}://{netloc.lower()}{path or '/'}" + (f"?{query}" if query else "")


def _seed_urls(root_source_url: str) -> list[str]:
    """Root URL plus the standard well-known discovery paths (RFC 9309, sitemaps.org)."""
    parsed = urlsplit(root_source_url)
    if parsed.scheme not in {"http", "https"}:
        return [root_source_url]
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return [root_source_url, f"{origin}/robots.txt", f"{origin}/sitemap.xml"]


def run_crawl(
    root_source_url: str,
    database_url: str,
    storage_dir: Path,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_artifacts: int = DEFAULT_MAX_ARTIFACTS,
    same_host_only: bool = True,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    allowed_content_types: frozenset[str] = DEFAULT_ALLOWED_CONTENT_TYPES,
) -> CrawlResult:
    root_host = urlsplit(root_source_url).netloc.lower()

    with connection(database_url) as conn:
        repository = AcquisitionRepository(conn)
        run = repository.create_run(root_source_url)
        conn.commit()

        visited: set[str] = set()
        queue: deque[_QueueItem] = deque(
            _QueueItem(url=url, depth=0, parent_event_id=None) for url in _seed_urls(root_source_url)
        )
        pages: list[CrawlPageResult] = []
        acquired_count = 0

        while queue:
            item = queue.popleft()
            normalized = _normalize(item.url)
            if normalized in visited:
                continue
            visited.add(normalized)

            if same_host_only and urlsplit(item.url).netloc.lower() != root_host:
                _record_failure(
                    repository, conn, run.id, "discovery", item.url,
                    f"off-host: discovery is restricted to {root_host}",
                    parent_event_id=item.parent_event_id,
                )
                continue

            if acquired_count >= max_artifacts:
                _record_failure(
                    repository, conn, run.id, "discovery", item.url,
                    f"max artifacts per run ({max_artifacts}) reached",
                    parent_event_id=item.parent_event_id,
                )
                continue

            # A per-URL failure (acquisition/unsupported-content/extraction) is recorded and
            # returned as an outcome so the crawl continues; a storage failure (raw filesystem
            # or database) is systemic and propagates out of this loop uncaught, aborting the run.
            outcome = _process_source(
                repository,
                conn,
                run.id,
                item.url,
                storage_dir,
                parent_event_id=item.parent_event_id,
                timeout_seconds=timeout_seconds,
                retries=retries,
                max_artifact_bytes=max_artifact_bytes,
                allowed_content_types=allowed_content_types,
            )

            pages.append(CrawlPageResult(item.url, item.depth, outcome.status, outcome.artifact, outcome.evidence))
            if outcome.artifact is not None:
                acquired_count += 1

            if outcome.content is None:
                continue

            # Bound structural depth per-link, not per-page: a rel="next" pagination link
            # stays at the same depth as the page it was found on (it is "more of the same
            # listing", not a deeper page), so pagination can continue past max_depth while
            # ordinary nested-page links cannot.
            base_url = outcome.resolved_url or item.url
            for link in discover_links(base_url, outcome.content, outcome.content_type or ""):
                next_depth = item.depth if link.is_pagination else item.depth + 1
                if next_depth > max_depth:
                    continue
                queue.append(_QueueItem(url=link.url, depth=next_depth, parent_event_id=outcome.acquisition_event_id))

        repository.complete_run(run.id, "succeeded")
        conn.commit()

        acquired_total = sum(1 for page in pages if page.artifact is not None)
        return CrawlResult(
            run_id=run.id,
            discovered=len(visited),
            acquired=acquired_total,
            rejected=len(visited) - acquired_total,
            pages=tuple(pages),
        )
