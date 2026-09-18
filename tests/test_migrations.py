from pathlib import Path
import tempfile
import unittest

from kpi_crawler.migrations import discover


class MigrationTests(unittest.TestCase):
    def test_discovers_and_hashes_migrations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "0001_example.sql"
            path.write_text("SELECT 1;", encoding="utf-8")
            migrations = discover(Path(directory))
            self.assertEqual(migrations[0].version, 1)
            self.assertEqual(len(migrations[0].checksum), 64)

    def test_rejects_invalid_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "bad.sql").write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaises(ValueError):
                discover(Path(directory))
