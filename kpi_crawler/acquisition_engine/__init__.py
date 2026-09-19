"""Program 1: the adaptive acquisition engine.

Produces raw artifacts, an acquisition evidence/provenance ledger, and
adaptive-decision history, exposed to any future consumer ("Program 2")
through a stable, versioned artifact/evidence contract (`contract.py`).

This package is independent of `kpi_crawler.acquisition`/`kpi_crawler.crawl`
(the existing single-threaded HTTP-only crawler feeding `app.evidence`
today) and of `kpi_crawler.extraction`. Nothing here is consumed by, or
consumes, those modules — the two acquisition paths coexist.
"""
