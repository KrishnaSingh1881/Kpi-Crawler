"""Generic web discovery mechanics: links, sitemaps, and robots.txt directives.

These are pure, structural parsing functions over already-acquired bytes: no
network I/O, no database access, and no judgment about which discovered URLs
are worth following. That decision (depth, host, dedup, budget) belongs to the
crawl orchestrator in `kpi_crawler.crawl`. Nothing here classifies a resource
by its filename or content, ranks it, or infers relevance; a link is a link.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit
from xml.etree import ElementTree


FOLLOWABLE_SCHEMES = frozenset({"http", "https", "file", ""})


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    is_pagination: bool


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.raw_links: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ("a", "link"):
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if href:
            self.raw_links.append((href, attributes.get("rel")))


def extract_html_links(html_bytes: bytes, base_url: str) -> tuple[DiscoveredLink, ...]:
    """Extract `<a href>`/`<link href>` targets, resolved against `base_url`.

    A `rel="next"` link is flagged as pagination (a real HTML/web standard),
    never inferred from link text, CSS classes, or position on the page.
    """
    parser = _LinkParser()
    parser.feed(html_bytes.decode("utf-8", errors="replace"))

    results: list[DiscoveredLink] = []
    seen: set[str] = set()
    for href, rel in parser.raw_links:
        absolute, _fragment = urldefrag(urljoin(base_url, href.strip()))
        if not absolute or absolute in seen:
            continue
        if urlsplit(absolute).scheme not in FOLLOWABLE_SCHEMES:
            continue
        seen.add(absolute)
        is_pagination = bool(rel) and "next" in rel.split()
        results.append(DiscoveredLink(url=absolute, is_pagination=is_pagination))
    return tuple(results)


def extract_sitemap_locations(xml_bytes: bytes) -> tuple[str, ...]:
    """Extract every `<loc>` entry from a sitemap or sitemap index document.

    A sitemap never legitimately needs a DOCTYPE or entity declaration, so any
    document containing one is rejected outright rather than parsed: stdlib
    `xml.etree.ElementTree` does not fetch external entities, but it does still
    expand internal ones, which a crawled (untrusted) host could otherwise use
    for an entity-expansion ("billion laughs") memory-exhaustion attack.
    """
    if b"<!DOCTYPE" in xml_bytes or b"<!ENTITY" in xml_bytes:
        return ()
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return ()
    locations = []
    for element in root.iter():
        local_tag = element.tag.rsplit("}", 1)[-1]
        if local_tag == "loc" and element.text and element.text.strip():
            locations.append(element.text.strip())
    return tuple(locations)


def extract_robots_sitemap_urls(robots_text: str) -> tuple[str, ...]:
    """Extract `Sitemap:` directive values from a robots.txt document (RFC 9309)."""
    urls = []
    for line in robots_text.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "sitemap" and value.strip():
            urls.append(value.strip())
    return tuple(urls)


def discover_links(base_url: str, content: bytes, content_type: str) -> tuple[DiscoveredLink, ...]:
    """Dispatch to the right parser by content type, and by the robots.txt well-known path."""
    if content_type == "text/html":
        return extract_html_links(content, base_url)
    if content_type in {"application/xml", "text/xml"}:
        return tuple(
            DiscoveredLink(url=urljoin(base_url, location), is_pagination=False)
            for location in extract_sitemap_locations(content)
        )
    if urlsplit(base_url).path.rstrip("/").endswith("/robots.txt"):
        return tuple(
            DiscoveredLink(url=urljoin(base_url, url), is_pagination=False)
            for url in extract_robots_sitemap_urls(content.decode("utf-8", errors="replace"))
        )
    return ()
