import os
import unittest
from unittest.mock import patch

from kpi_crawler.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_loads_required_settings(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://example", "LOG_LEVEL": "debug"}, clear=True):
            self.assertEqual(Settings.from_environment(), Settings("postgresql://example", "DEBUG"))

    def test_requires_database_url(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_loads_acquisition_limit_overrides(self):
        env = {
            "DATABASE_URL": "postgresql://example",
            "ACQUISITION_TIMEOUT_SECONDS": "5",
            "ACQUISITION_RETRIES": "0",
            "MAX_ARTIFACT_BYTES": "1000",
            "ALLOWED_CONTENT_TYPES": "application/pdf, text/plain",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_environment()
        self.assertEqual(settings.acquisition_timeout_seconds, 5.0)
        self.assertEqual(settings.acquisition_retries, 0)
        self.assertEqual(settings.max_artifact_bytes, 1000)
        self.assertEqual(settings.allowed_content_types, frozenset({"application/pdf", "text/plain"}))

    def test_rejects_non_positive_timeout(self):
        env = {"DATABASE_URL": "postgresql://example", "ACQUISITION_TIMEOUT_SECONDS": "0"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_rejects_negative_retries(self):
        env = {"DATABASE_URL": "postgresql://example", "ACQUISITION_RETRIES": "-1"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_rejects_non_positive_max_artifact_bytes(self):
        env = {"DATABASE_URL": "postgresql://example", "MAX_ARTIFACT_BYTES": "0"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_rejects_empty_allowed_content_types(self):
        env = {"DATABASE_URL": "postgresql://example", "ALLOWED_CONTENT_TYPES": "  ,  "}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()
