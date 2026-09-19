# Retrieval Baseline Observations

Observations from the semantic retrieval baseline (`kpi-crawler-retrieval`), run
`run-20260919T021432Z`, against the real 8-PDF NIRF fixture corpus (3,547
evidence rows) and the real 229-KPI dictionary
(`DOC-20260901-WA0019.xlsx`). Full results: `reports/run-20260919T021432Z/`.

These are observations only. Nothing below was acted on — the baseline
(embedding, storage, retrieval) was left exactly as measured, per this phase's
scope.

## 1. A small set of "god KPIs" dominate evidence→KPI retrieval

Within representation A (`canonical_text`), four KPIs — `X05` (NIRF rank),
`X04` (THE rank), `X03` (QS rank), `X01` (a fourth cross-cutting ranking KPI)
— appear in the top-10 for the overwhelming majority of **all 3,545 `text`**
evidence rows (`X05` alone: 3,544 of 3,545 possible hits; see report §12).
This is not because most evidence is actually about institutional rankings —
it is because most `canonical_text` values share the literal prefix
`"National Institutional Ranking Framework: ..."` (see #3 below), and that
phrase itself is semantically closest to the ranking-related KPI
definitions. The retrieval is doing exactly what cosine similarity on that
input should do; the input is the problem, not the retrieval.

## 2. Representation A and B diverge sharply and unpredictably per row

For most evidence rows, the top-5 KPIs returned by representation A
(`canonical_text`) and representation B (`supporting_context`) are
completely disjoint (see report §13, e.g. evidence 119, 237, 355). A tends
toward the same handful of ranking/strategic KPIs regardless of content; B
is noisier but sometimes more topically plausible (e.g. evidence 237,
`"students admitted in"`, B's top-5 are all student-count KPIs `S10/S11/
S03/S04/S02`, while A's top-5 are ranking KPIs). Neither representation
consistently "wins" — B looks better exactly on the rows where A's fixed
prefix swamps the actual content, and worse on very short fragments where B
has almost no signal at all (see #4).

## 3. The canonical_text prefix is uninformative for nearly all text evidence

This confirms and quantifies the limitation already flagged in `progress.md`
("the heading/section heuristic ... fired exactly once per real document").
Concretely: `canonical_text` for ordinary text evidence is
`"National Institutional Ranking Framework: <line>"` for the vast majority
of rows, because the page-1 title is the only heading ever detected across
all 8 real, densely-formatted documents. The prefix adds no discriminating
information and appears to actively pull every embedding toward the
ranking-KPI region of the embedding space.

## 4. Extreme text duplication — independent of canonical_text framing

Of 3,547 evidence rows, 2,332 (66%) are an exact duplicate of another row's
text — and this holds for **both** `canonical_text` and `supporting_context`
equally (same duplicate-row count for each), so the duplication is in the
underlying extracted line fragments themselves, not an artifact of
serialization. Examples: `"National Institutional Ranking Framework: No. of
students"` appears 106 times; `"...: 2021-22"` 90 times; `"...: 0"` 77
times; `"...: -"` 57 times. These are short, low-information line-level
fragments (a bare year, a bare dash, a bare zero) that recur across a
tabular government-form document. Identical text naturally produces
identical embeddings and identical similarity scores — visible directly in
the KPI-sample section of the report (e.g. `A01`'s top-5 canonical_text
matches are all similarity `0.7416` exactly, five different evidence rows
tied at the same score). This is a granularity/dedup characteristic of the
existing extraction output, not a retrieval defect.

## 5. Table and (absent) visual evidence get essentially no differentiated treatment

Only 2 of 3,547 evidence rows are `table` type (matching the pre-existing,
documented table-detection coverage gap on rotated real PDFs — see
`progress.md`), and 0 are `visual` — this particular 8-document fixture set
produced no extractable bitmap evidence, so representation comparisons for
`visual` could not be observed in this run. The 2 table rows *were* embedded
and retrieved like any other evidence (no filtering, per phase scope), but
two rows is too small a sample to say anything about table-representation
quality specifically; report §12 shows their top-10 hits are spread over 10
distinct KPIs with no obvious pattern.

## 6. KPI→evidence direction returns 100% `text` evidence in the sample, unsurprisingly

Since `text` is 3,545 of 3,547 evidence rows (99.9%), every KPI's top-10
nearest evidence is `text` in both representations (report §12: 100.0% for
both A and B). This is the base-rate outcome, not a sign that `table`
evidence is being suppressed — there simply isn't enough non-text evidence
in this corpus for the base rate to look different.

## 7. Cross-KPI ties within a single evidence type are common on representation A

Beyond the exact-duplicate-text ties (#4), several *different* KPIs
targeting near-identical, generic evidence (e.g. `F17`/`F06`, both
faculty-related, retrieving the same five evidence rows in nearly the same
order — report §14) suggests the embedding space compresses a lot of this
corpus's short, form-label-style text into a small number of regions,
independent of which specific KPI a human would map it to.

## 8. Practical implication for a future mapping phase (not acted on here)

If a future phase builds on this baseline, the biggest lever is likely
upstream of embedding choice entirely: de-duplicating identical short
fragments before embedding (or embedding unique texts once and reusing the
vector) would cut embedding work by ~66% and remove one obvious source of
misleading similarity ties — but that is a change to extraction/evidence
handling, explicitly out of scope for this phase, and is recorded here only
as an observation.
