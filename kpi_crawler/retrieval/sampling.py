"""Deterministic sampling for the human-readable report.

No randomness and no hand-picking: every sample is a pure function of the
input list's order and size, so the same database state always produces the
same sample.
"""

from .kpi_dictionary import KPIDefinition
from .store import EvidenceRow


def _evenly_spaced_indices(size: int, count: int) -> list[int]:
    if size <= 0 or count <= 0:
        return []
    count = min(count, size)
    return sorted({(i * size) // count for i in range(count)})


def _largest_remainder_allocation(group_sizes: list[int], total: int) -> list[int]:
    """Proportionally allocate `total` slots across groups by size (largest-remainder method)."""
    grand_total = sum(group_sizes)
    if grand_total == 0:
        return [0 for _ in group_sizes]
    total = min(total, grand_total)
    exact = [size * total / grand_total for size in group_sizes]
    base = [min(int(value), size) for value, size in zip(exact, group_sizes)]
    remainder = total - sum(base)
    # Give leftover slots to the groups with the largest fractional remainder,
    # breaking ties by group order (deterministic), skipping already-exhausted groups.
    order = sorted(range(len(group_sizes)), key=lambda i: (-(exact[i] - base[i]), i))
    for i in order:
        if remainder <= 0:
            break
        if base[i] < group_sizes[i]:
            base[i] += 1
            remainder -= 1
    return base


def deterministic_evidence_sample(rows: list[EvidenceRow], n: int = 30) -> list[EvidenceRow]:
    """A deterministic sample of up to n evidence rows, stratified across evidence_type."""
    types = sorted({row.evidence_type for row in rows})
    groups = {evidence_type: [row for row in rows if row.evidence_type == evidence_type] for evidence_type in types}
    allocation = _largest_remainder_allocation([len(groups[t]) for t in types], n)

    sample: list[EvidenceRow] = []
    for evidence_type, count in zip(types, allocation):
        group = groups[evidence_type]
        for idx in _evenly_spaced_indices(len(group), count):
            sample.append(group[idx])
    sample.sort(key=lambda row: row.evidence_id)
    return sample


def deterministic_kpi_sample(kpis: list[KPIDefinition], n: int = 20) -> list[KPIDefinition]:
    """A deterministic sample of up to n KPIs, evenly spaced across the sorted dictionary."""
    ordered = sorted(kpis, key=lambda kpi: kpi.kpi_code)
    return [ordered[idx] for idx in _evenly_spaced_indices(len(ordered), n)]
