import unittest

from kpi_crawler.acquisition_engine.browser_acquirer import dynamic_content_likely

SPA_SHELL = b"""
<!DOCTYPE html><html><head>
<script src="/static/js/main.abcd1234.js"></script>
<script src="/static/js/vendor.efgh5678.js"></script>
</head><body><div id="root"></div></body></html>
"""

RICH_HTML = b"""
<!DOCTYPE html><html><head><title>Report</title></head><body>
<h1>Annual Report 2024</h1>
<p>This institution enrolled 4,201 undergraduate students across 42 programmes
in the reporting year, an increase of 6%% over the prior year. Total research
output included 128 peer-reviewed publications and 14 patents filed.</p>
<table><tr><th>Metric</th><th>Value</th></tr><tr><td>Enrollment</td><td>4201</td></tr></table>
</body></html>
"""


class DynamicContentDetectionTests(unittest.TestCase):
    def test_detects_script_heavy_near_empty_shell(self):
        self.assertTrue(dynamic_content_likely(SPA_SHELL, "text/html"))

    def test_does_not_flag_content_rich_html(self):
        self.assertFalse(dynamic_content_likely(RICH_HTML, "text/html"))

    def test_ignores_non_html_content_types(self):
        self.assertFalse(dynamic_content_likely(SPA_SHELL, "application/pdf"))

    def test_handles_empty_content(self):
        self.assertFalse(dynamic_content_likely(b"", "text/html"))

    def test_handles_missing_content_type(self):
        self.assertFalse(dynamic_content_likely(SPA_SHELL, None))


if __name__ == "__main__":
    unittest.main()
