from datetime import datetime, timezone
from pathlib import Path
import unittest

from kpi_crawler.extraction import PdfExtractor
from kpi_crawler.models import Artifact


FIXTURE = Path(__file__).parent / "fixtures" / "structured.pdf"


class PdfExtractorTests(unittest.TestCase):
    def test_preserves_text_table_and_visual_structure(self):
        artifact = Artifact(
            id=1,
            sha256="0" * 64,
            raw_storage_ref=str(FIXTURE),
            source_url="file:///structured.pdf",
            source_type="file",
            content_type="application/pdf",
            retrieved_at=datetime.now(timezone.utc),
            byte_size=FIXTURE.stat().st_size,
            created_at=datetime.now(timezone.utc),
        )
        result = PdfExtractor().extract(artifact)

        self.assertEqual(result.artifact_id, artifact.id)
        self.assertEqual(result.metadata["page_count"], 1)
        heading = next(item for item in result.items if item.structure["role"] == "heading")
        table = next(item for item in result.items if item.evidence_type == "table")
        visual = next(item for item in result.items if item.evidence_type == "visual")

        self.assertEqual(heading.supporting_context, "Quarterly Report")
        self.assertEqual(table.structure["headers"], ["Metric", "Value"])
        self.assertEqual(table.structure["rows"], [["Revenue", "120"], ["Margin", "30"]])
        self.assertEqual(table.page_number, 1)
        self.assertIsNotNone(table.location)
        self.assertEqual(visual.structure["kind"], "bitmap")
        self.assertEqual(visual.page_number, 1)
