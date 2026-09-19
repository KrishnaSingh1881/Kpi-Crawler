import unittest

from kpi_crawler.retrieval.kpi_dictionary import KPIDefinition
from kpi_crawler.retrieval.sampling import deterministic_evidence_sample, deterministic_kpi_sample
from kpi_crawler.retrieval.store import EvidenceRow


def _evidence_row(evidence_id: int, evidence_type: str) -> EvidenceRow:
    return EvidenceRow(
        evidence_id=evidence_id,
        artifact_id=1,
        evidence_type=evidence_type,
        canonical_text=f"text {evidence_id}",
        supporting_context=f"context {evidence_id}",
        canonical_serializer_version="1",
    )


def _kpi(code: str) -> KPIDefinition:
    return KPIDefinition(
        kpi_code=code,
        variable_name=f"name {code}",
        definition=f"definition {code}",
        domain=None,
        parameter=None,
        sub_parameter=None,
        unit=None,
        data_type=None,
        formula=None,
        primary_source=None,
        benchmark_direction=None,
        priority_12m=None,
        source_file="test.xlsx",
        source_row={},
    )


class DeterministicEvidenceSampleTests(unittest.TestCase):
    def test_reproducible_on_repeated_calls(self):
        rows = [_evidence_row(i, "text" if i % 5 else "table") for i in range(1, 101)]
        first = deterministic_evidence_sample(rows, 30)
        second = deterministic_evidence_sample(rows, 30)
        self.assertEqual([r.evidence_id for r in first], [r.evidence_id for r in second])

    def test_spans_all_present_evidence_types(self):
        rows = (
            [_evidence_row(i, "text") for i in range(1, 91)]
            + [_evidence_row(i, "table") for i in range(91, 99)]
            + [_evidence_row(i, "visual") for i in range(99, 101)]
        )
        sample = deterministic_evidence_sample(rows, 30)
        types_present = {row.evidence_type for row in sample}
        self.assertEqual(types_present, {"text", "table", "visual"})

    def test_never_exceeds_requested_count(self):
        rows = [_evidence_row(i, "text") for i in range(1, 6)]
        sample = deterministic_evidence_sample(rows, 30)
        self.assertEqual(len(sample), 5)

    def test_empty_input_returns_empty_sample(self):
        self.assertEqual(deterministic_evidence_sample([], 30), [])


class DeterministicKPISampleTests(unittest.TestCase):
    def test_reproducible_and_bounded(self):
        kpis = [_kpi(f"A{i:02d}") for i in range(1, 230)]
        first = deterministic_kpi_sample(kpis, 20)
        second = deterministic_kpi_sample(kpis, 20)
        self.assertEqual(len(first), 20)
        self.assertEqual([k.kpi_code for k in first], [k.kpi_code for k in second])

    def test_evenly_spaced_across_sorted_codes(self):
        kpis = [_kpi(f"A{i:03d}") for i in range(1, 230)]
        sample = deterministic_kpi_sample(kpis, 20)
        codes = [k.kpi_code for k in sample]
        self.assertEqual(codes, sorted(codes))
        self.assertEqual(codes[0], "A001")


if __name__ == "__main__":
    unittest.main()
