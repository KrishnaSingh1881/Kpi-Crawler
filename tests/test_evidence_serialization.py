import unittest

from kpi_crawler.evidence_serialization import (
    CANONICAL_SERIALIZER_VERSION,
    serialize_evidence,
)


class EvidenceSerializationTests(unittest.TestCase):
    def test_text_serialization_is_deterministic(self):
        args = ("KPI", "42", "KPI: 42", {"role": "text", "section": "Overview"})
        self.assertEqual(serialize_evidence("text", *args), serialize_evidence("text", *args))

    def test_text_serialization_includes_section_context(self):
        result = serialize_evidence(
            "text", "KPI", "42", "KPI: 42", {"role": "text", "section": "Overview"}
        )
        self.assertEqual(result, "Overview: KPI: 42")

    def test_text_heading_serialization_omits_redundant_section(self):
        result = serialize_evidence(
            "text", "Quarterly Report", "Quarterly Report", "Quarterly Report",
            {"role": "heading", "section": "Quarterly Report"},
        )
        self.assertEqual(result, "Quarterly Report")

    def test_text_serialization_without_section(self):
        result = serialize_evidence("text", "KPI", "42", "KPI: 42", {"role": "text", "section": None})
        self.assertEqual(result, "KPI: 42")

    def test_table_serialization_is_deterministic_and_minimal(self):
        structure = {"headers": ["Metric", "Value"], "rows": [["Revenue", "120"]], "section": "Financials"}
        context = "Metric | Value\nRevenue | 120"
        result = serialize_evidence("table", "Table", "Metric | Value", context, structure)
        self.assertEqual(result, "Financials: Table — Metric | Value\nRevenue | 120")
        self.assertEqual(
            result,
            serialize_evidence("table", "Table", "Metric | Value", context, structure),
        )

    def test_visual_serialization_is_deterministic(self):
        structure = {"kind": "bitmap", "section": "Overview"}
        context = "Embedded image on page 1"
        result = serialize_evidence("visual", "Embedded image", "bitmap", context, structure)
        self.assertEqual(result, "Overview: Embedded image on page 1")
        self.assertEqual(
            result,
            serialize_evidence("visual", "Embedded image", "bitmap", context, structure),
        )

    def test_unknown_evidence_type_is_rejected(self):
        with self.assertRaises(ValueError):
            serialize_evidence("unknown", "c", "v", "s", {})

    def test_serializer_has_a_version(self):
        self.assertTrue(CANONICAL_SERIALIZER_VERSION)
