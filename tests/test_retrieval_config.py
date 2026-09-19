import os
import unittest
from unittest.mock import patch

from kpi_crawler.config import ConfigurationError
from kpi_crawler.retrieval.config import RetrievalSettings


class RetrievalSettingsTests(unittest.TestCase):
    def test_requires_database_url(self):
        with patch.dict(os.environ, {"KPI_DICTIONARY_PATH": "/tmp/kpis.xlsx"}, clear=True):
            with self.assertRaises(ConfigurationError):
                RetrievalSettings.from_environment()

    def test_requires_kpi_dictionary_path(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://example"}, clear=True):
            with self.assertRaises(ConfigurationError):
                RetrievalSettings.from_environment()

    def test_loads_defaults(self):
        env = {"DATABASE_URL": "postgresql://example", "KPI_DICTIONARY_PATH": "/tmp/kpis.xlsx"}
        with patch.dict(os.environ, env, clear=True):
            settings = RetrievalSettings.from_environment()
        self.assertEqual(settings.top_k, 10)
        self.assertEqual(settings.embedding_dimension, 2560)
        self.assertEqual(settings.embedding_model, "qwen3-embedding:4b")

    def test_rejects_non_positive_top_k(self):
        env = {
            "DATABASE_URL": "postgresql://example",
            "KPI_DICTIONARY_PATH": "/tmp/kpis.xlsx",
            "RETRIEVAL_TOP_K": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigurationError):
                RetrievalSettings.from_environment()

    def test_rejects_non_positive_embedding_dimension(self):
        env = {
            "DATABASE_URL": "postgresql://example",
            "KPI_DICTIONARY_PATH": "/tmp/kpis.xlsx",
            "EMBEDDING_DIMENSION": "-1",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigurationError):
                RetrievalSettings.from_environment()


if __name__ == "__main__":
    unittest.main()
