import os
import unittest
from unittest.mock import Mock, patch

from kpi_crawler.acquisition import AcquisitionError
from kpi_crawler.cli import build_parser, main
from kpi_crawler.errors import (
    DatabaseError,
    ExportError,
    ExtractionError,
    OperationalLimitError,
    StorageError,
    UnsupportedContentError,
)

ENV = {"DATABASE_URL": "postgresql://example"}


class BuildParserTests(unittest.TestCase):
    def test_accepts_all_known_commands(self):
        for command in ("start", "migrate", "process", "crawl", "export"):
            args = build_parser().parse_args([command, "http://example.test"])
            self.assertEqual(args.command, command)
            self.assertEqual(args.source_url, "http://example.test")

    def test_defaults_to_start_with_no_arguments(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.command, "start")
        self.assertIsNone(args.source_url)

    def test_rejects_unknown_command(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["bogus"])


class MainConfigurationTests(unittest.TestCase):
    def test_missing_database_url_returns_1_without_raising(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main([]), 1)

    def test_process_without_source_url_returns_1(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(main(["process"]), 1)

    def test_crawl_without_source_url_returns_1(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(main(["crawl"]), 1)

    def test_export_without_run_id_returns_1(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(main(["export"]), 1)

    def test_export_with_non_numeric_run_id_returns_1(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(main(["export", "not-a-number"]), 1)


class MainProcessCommandTests(unittest.TestCase):
    def _mock_result(self):
        result = Mock()
        result.artifact.sha256 = "deadbeef"
        result.evidence = [Mock(), Mock()]
        return result

    def test_success_returns_0(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_pdf_pipeline", return_value=self._mock_result()) as pipeline:
                self.assertEqual(main(["process", "http://example.test/a.pdf"]), 0)
        pipeline.assert_called_once()

    def test_acquisition_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_pdf_pipeline", side_effect=AcquisitionError("boom")):
                self.assertEqual(main(["process", "http://example.test/a.pdf"]), 1)

    def test_unsupported_content_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_pdf_pipeline", side_effect=UnsupportedContentError("boom")):
                self.assertEqual(main(["process", "http://example.test/a.pdf"]), 1)

    def test_operational_limit_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_pdf_pipeline", side_effect=OperationalLimitError("boom")):
                self.assertEqual(main(["process", "http://example.test/a.pdf"]), 1)

    def test_extraction_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_pdf_pipeline", side_effect=ExtractionError("boom")):
                self.assertEqual(main(["process", "http://example.test/a.pdf"]), 1)

    def test_storage_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_pdf_pipeline", side_effect=StorageError("boom")):
                self.assertEqual(main(["process", "http://example.test/a.pdf"]), 1)

    def test_database_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_pdf_pipeline", side_effect=DatabaseError("boom")):
                self.assertEqual(main(["process", "http://example.test/a.pdf"]), 1)


class MainCrawlCommandTests(unittest.TestCase):
    def _mock_crawl_result(self):
        result = Mock()
        result.discovered = 3
        result.acquired = 2
        result.rejected = 1
        result.run_id = 7
        return result

    def test_success_returns_0(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_crawl", return_value=self._mock_crawl_result()) as crawl:
                self.assertEqual(main(["crawl", "http://example.test/"]), 0)
        crawl.assert_called_once()

    def test_database_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.run_crawl", side_effect=DatabaseError("boom")):
                self.assertEqual(main(["crawl", "http://example.test/"]), 1)


class MainExportCommandTests(unittest.TestCase):
    def _mock_export_result(self):
        result = Mock()
        result.run_id = 4
        result.output_dir = "/tmp/exports/run-4"
        result.source_count = 2
        result.evidence_count = 10
        result.rejected_count = 1
        return result

    def test_success_returns_0_and_parses_numeric_run_id(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.export_run", return_value=self._mock_export_result()) as export:
                self.assertEqual(main(["export", "4"]), 0)
        export.assert_called_once()
        self.assertEqual(export.call_args[0][0], 4)

    def test_export_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.export_run", side_effect=ExportError("run 999 does not exist")):
                self.assertEqual(main(["export", "999"]), 1)

    def test_database_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.export_run", side_effect=DatabaseError("boom")):
                self.assertEqual(main(["export", "4"]), 1)


class MainMigrateCommandTests(unittest.TestCase):
    def test_success_returns_0(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.migrate", return_value=3) as migrate:
                self.assertEqual(main(["migrate"]), 0)
        migrate.assert_called_once()

    def test_database_error_returns_1_not_raised(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch("kpi_crawler.cli.migrate", side_effect=DatabaseError("boom")):
                self.assertEqual(main(["migrate"]), 1)


if __name__ == "__main__":
    unittest.main()
