import unittest

from kpi_crawler.discovery import (
    discover_links,
    extract_html_links,
    extract_robots_sitemap_urls,
    extract_sitemap_locations,
)


class ExtractHtmlLinksTests(unittest.TestCase):
    def test_resolves_relative_links_against_base_url(self):
        html = b'<html><body><a href="/about">About</a></body></html>'
        links = extract_html_links(html, "https://example.test/dir/page.html")
        self.assertEqual([link.url for link in links], ["https://example.test/about"])

    def test_strips_fragments_and_deduplicates(self):
        html = b"""
        <a href="/page">One</a>
        <a href="/page#section">Same page, different fragment</a>
        """
        links = extract_html_links(html, "https://example.test/")
        self.assertEqual([link.url for link in links], ["https://example.test/page"])

    def test_flags_rel_next_as_pagination(self):
        html = b'<a href="/list?page=2" rel="next">Next</a><a href="/list?page=1">Page 1</a>'
        links = extract_html_links(html, "https://example.test/list")
        pagination = {link.url: link.is_pagination for link in links}
        self.assertTrue(pagination["https://example.test/list?page=2"])
        self.assertFalse(pagination["https://example.test/list?page=1"])

    def test_does_not_infer_pagination_from_link_text_or_class(self):
        html = b'<a href="/next-page" class="next">Next page</a>'
        links = extract_html_links(html, "https://example.test/")
        self.assertFalse(links[0].is_pagination)

    def test_drops_non_followable_schemes(self):
        html = b"""
        <a href="mailto:someone@example.test">Email</a>
        <a href="javascript:void(0)">JS</a>
        <a href="/real-page">Real</a>
        """
        links = extract_html_links(html, "https://example.test/")
        self.assertEqual([link.url for link in links], ["https://example.test/real-page"])

    def test_extracts_link_rel_next_from_head(self):
        html = b'<head><link rel="next" href="/page/2"></head>'
        links = extract_html_links(html, "https://example.test/page/1")
        self.assertEqual(links[0].url, "https://example.test/page/2")
        self.assertTrue(links[0].is_pagination)

    def test_does_not_filter_by_filename(self):
        html = b'<a href="/report.pdf">Report</a><a href="/notes.docx">Notes</a>'
        links = extract_html_links(html, "https://example.test/")
        self.assertEqual(
            {link.url for link in links},
            {"https://example.test/report.pdf", "https://example.test/notes.docx"},
        )

    def test_flags_stylesheet_and_icon_links_as_resource_references(self):
        html = b"""
        <link rel="stylesheet" href="/theme.css">
        <link rel="shortcut icon" href="/favicon.ico">
        <a href="/about">About</a>
        """
        links = extract_html_links(html, "https://example.test/")
        by_url = {link.url: link.is_resource_reference for link in links}
        self.assertTrue(by_url["https://example.test/theme.css"])
        self.assertTrue(by_url["https://example.test/favicon.ico"])
        self.assertFalse(by_url["https://example.test/about"])

    def test_does_not_infer_resource_reference_from_extension(self):
        # A .css URL with no rel attribute at all (e.g. an <a href>) is not
        # flagged — the signal is the standards-defined rel value, never the
        # filename.
        html = b'<a href="/report.css">Report</a>'
        links = extract_html_links(html, "https://example.test/")
        self.assertFalse(links[0].is_resource_reference)


class ExtractSitemapLocationsTests(unittest.TestCase):
    def test_extracts_urlset_locations(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.test/a</loc></url>
            <url><loc>https://example.test/b</loc></url>
        </urlset>"""
        self.assertEqual(
            extract_sitemap_locations(xml),
            ("https://example.test/a", "https://example.test/b"),
        )

    def test_extracts_sitemap_index_locations(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <sitemap><loc>https://example.test/sitemap-a.xml</loc></sitemap>
        </sitemapindex>"""
        self.assertEqual(extract_sitemap_locations(xml), ("https://example.test/sitemap-a.xml",))

    def test_malformed_xml_returns_empty(self):
        self.assertEqual(extract_sitemap_locations(b"not xml"), ())


class ExtractRobotsSitemapUrlsTests(unittest.TestCase):
    def test_extracts_sitemap_directive(self):
        robots = "User-agent: *\nDisallow: /private\nSitemap: https://example.test/sitemap.xml\n"
        self.assertEqual(extract_robots_sitemap_urls(robots), ("https://example.test/sitemap.xml",))

    def test_no_sitemap_directive_returns_empty(self):
        self.assertEqual(extract_robots_sitemap_urls("User-agent: *\nDisallow: /\n"), ())

    def test_multiple_sitemap_directives(self):
        robots = "Sitemap: https://example.test/a.xml\nSitemap: https://example.test/b.xml\n"
        self.assertEqual(
            extract_robots_sitemap_urls(robots),
            ("https://example.test/a.xml", "https://example.test/b.xml"),
        )


class DiscoverLinksDispatchTests(unittest.TestCase):
    def test_dispatches_html_to_link_extraction(self):
        html = b'<a href="/child">Child</a>'
        links = discover_links("https://example.test/", html, "text/html")
        self.assertEqual([link.url for link in links], ["https://example.test/child"])

    def test_dispatches_xml_to_sitemap_extraction(self):
        xml = b"<urlset><url><loc>https://example.test/x</loc></url></urlset>"
        links = discover_links("https://example.test/sitemap.xml", xml, "application/xml")
        self.assertEqual([link.url for link in links], ["https://example.test/x"])

    def test_dispatches_robots_txt_path_to_sitemap_directive_extraction(self):
        robots = b"Sitemap: https://example.test/sitemap.xml\n"
        links = discover_links("https://example.test/robots.txt", robots, "text/plain")
        self.assertEqual([link.url for link in links], ["https://example.test/sitemap.xml"])

    def test_unrecognized_content_type_yields_no_links(self):
        links = discover_links("https://example.test/report.pdf", b"%PDF-1.4", "application/pdf")
        self.assertEqual(links, ())


if __name__ == "__main__":
    unittest.main()
