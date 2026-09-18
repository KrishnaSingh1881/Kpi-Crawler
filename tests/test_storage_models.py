import hashlib
import unittest

from kpi_crawler.storage import _utc_now


class StorageModelTests(unittest.TestCase):
    def test_sha256_is_content_identity(self):
        self.assertEqual(
            hashlib.sha256(b"payload").hexdigest(),
            "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
        )

    def test_now_is_timezone_aware(self):
        self.assertIsNotNone(_utc_now().tzinfo)
