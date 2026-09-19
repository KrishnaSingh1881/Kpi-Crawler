# Retrieval Baseline Report — run-20260919T021432Z

Pure top-K cosine retrieval baseline: existing evidence, embedded as-is with a single fixed model, stored in pgvector, queried both directions. No reranking, filtering, thresholding, or relevance scoring of any kind. This is a measurement, not a mapping system.

## 1. Run metadata

- Run ID: `run-20260919T021432Z`
- Started (UTC): 2026-09-19T02:14:32.205473+00:00
- Finished (UTC): 2026-09-19T02:17:58.577386+00:00
- KPI source file: `/home/krishna/Kpi-Crawler/DOC-20260901-WA0019.xlsx`
- Database: `localhost:5433/kpi_crawler` (credentials redacted)
- Retrieval K: 10
- Similarity metric: cosine (pgvector `<=>` cosine distance; similarity = 1 - distance)

## 2-4. Model, embedding dimension, database/vector configuration

- Model identifier: `qwen3-embedding:4b`
- Runtime: Ollama (local daemon, GGUF/llama.cpp backend), 100% GPU-resident on this machine
- Underlying model: Qwen3-Embedding-4B, Q4_K_M GGUF quantization (~4.4 GB resident)
  - Reason for quantized GGUF instead of the raw bf16 safetensors: this machine's GPU has 8 GB total VRAM (~6.6 GB free at run time); the official bf16 weights alone need ~8 GB and do not fit. Ollama already had this exact model available as a Q4_K_M GGUF build, which is a runtime/quantization change, not a model substitution — still Qwen3-Embedding-4B.
- Embedding endpoint: `http://localhost:11434/api/embed` (local loopback only)
- Embedding dimension: 2560
- Normalization applied: True (L2 normalization applied client-side after retrieval from the runtime)
- Fixed embedding instruction: None (none — same for evidence and KPI text)
- Batch size: 64
- Vector storage: PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16`), `vector(2560)` columns
- Vector/index information: exact (brute-force) cosine search via `ORDER BY embedding <=> query LIMIT k`; no ANN index (HNSW/IVFFlat) — at ~3.5K evidence rows and 229 KPI rows, an exact scan is fast and removes approximate-index recall as a confound in this baseline measurement.
  - `app.kpi_embeddings`: ~229 rows, 5832 kB on disk
  - `app.evidence_embeddings`: ~7094 rows, 172 MB on disk

## 5-7. Corpus counts

- KPI count (loaded from dictionary): 229
- Evidence count (all types, no filtering): 3547
- Evidence count by type:
  - `table`: 2
  - `text`: 3545

## 8-9. Timings

- Embedding time, KPIs (229 texts): 8.87s
- Embedding time, evidence A / canonical_text (3547 texts): 89.59s
- Embedding time, evidence B / supporting_context (3547 texts): 89.43s
- Total embedding time: 187.89s
- Total texts embedded: 7323 (25.7 ms/text average)
- Retrieval time, evidence→KPI experiment (7094 queries, A+B): 9.17s
  - 1.29 ms/query average
- Retrieval time, KPI→evidence experiment (458 queries, A+B): 7.33s
  - 16.01 ms/query average

## 10-11. Result row counts

- `evidence_to_kpi_A.jsonl`: 35470 rows (3547 evidence x up to 10)
- `evidence_to_kpi_B.jsonl`: 35470 rows
- `kpi_to_evidence_A.jsonl`: 2290 rows (229 KPIs x up to 10)
- `kpi_to_evidence_B.jsonl`: 2290 rows

## 12. Top-10 frequency summaries

### KPI hit frequency within evidence→KPI (representation A), by evidence_type

- `table` (20 total hits across top-10):
  - X05: 2
  - I19: 2
  - S23: 2
  - I16: 2
  - INT03: 2
  - F11: 2
  - F05: 2
  - S22: 2
  - X06: 2
  - ESG10: 1
- `text` (35450 total hits across top-10):
  - X05: 3544
  - X04: 3349
  - X03: 3263
  - X01: 3001
  - F08: 2519
  - R08: 2402
  - R05: 2386
  - A02: 1855
  - R07: 1149
  - R23: 1028

### Evidence-type frequency within KPI→evidence top-10 results

- Representation canonical_text (2290 total result rows):
  - `text`: 2290 (100.0%)
- Representation supporting_context (2290 total result rows):
  - `text`: 2290 (100.0%)

## 13. Evidence sample (30, deterministic, stratified by evidence_type)

### Evidence sample -> top-10 KPI (representation A vs B)

**Evidence 1** (`text`, artifact 1)
- canonical_text: National Institutional Ranking Framework
- supporting_context: National Institutional Ranking Framework
  - A (canonical_text) top-5: X05 (0.869); X04 (0.761); X03 (0.745); X01 (0.727); R08 (0.709)
  - B (supporting_context) top-5: X05 (0.870); X04 (0.763); X03 (0.747); X01 (0.725); R08 (0.708)

**Evidence 119** (`text`, artifact 1)
- canonical_text: National Institutional Ranking Framework: 13
- supporting_context: 13
  - A (canonical_text) top-5: X05 (0.832); X04 (0.734); X03 (0.716); X01 (0.706); F08 (0.686)
  - B (supporting_context) top-5: X04 (0.516); X14 (0.504); ESG02 (0.494); ESG05 (0.472); I18 (0.472)

**Evidence 237** (`text`, artifact 1)
- canonical_text: National Institutional Ranking Framework: students admitted in
- supporting_context: students admitted in
  - A (canonical_text) top-5: X05 (0.810); S05 (0.715); S12 (0.715); X03 (0.714); FIN04 (0.709)
  - B (supporting_context) top-5: S10 (0.739); S11 (0.739); S03 (0.726); S04 (0.719); S02 (0.707)

**Evidence 355** (`text`, artifact 1)
- canonical_text: National Institutional Ranking Framework: Thousand Eight Hundred Ninety Three )
- supporting_context: Thousand Eight Hundred Ninety Three )
  - A (canonical_text) top-5: X05 (0.787); X04 (0.697); X03 (0.653); F08 (0.632); X01 (0.628)
  - B (supporting_context) top-5: ESG02 (0.530); X04 (0.525); G03 (0.508); A06 (0.507); I16 (0.505)

**Evidence 473** (`text`, artifact 2)
- canonical_text: National Institutional Ranking Framework: & female)
- supporting_context: & female)
  - A (canonical_text) top-5: X05 (0.776); X04 (0.696); ESG13 (0.685); X03 (0.677); X01 (0.666)
  - B (supporting_context) top-5: ESG13 (0.631); S07 (0.623); X04 (0.562); ESG02 (0.560); F08 (0.553)

**Evidence 591** (`text`, artifact 2)
- canonical_text: National Institutional Ranking Framework: 74
- supporting_context: 74
  - A (canonical_text) top-5: X05 (0.851); X04 (0.725); X03 (0.721); X01 (0.702); R08 (0.688)
  - B (supporting_context) top-5: X04 (0.518); X14 (0.503); F08 (0.503); R05 (0.494); INT05 (0.493)

**Evidence 710** (`text`, artifact 2)
- canonical_text: National Institutional Ranking Framework: Ph.D (Student pursuing doctoral program till 2023-24)
- supporting_context: Ph.D (Student pursuing doctoral program till 2023-24)
  - A (canonical_text) top-5: X05 (0.725); A03 (0.697); R24 (0.693); R25 (0.682); R26 (0.673)
  - B (supporting_context) top-5: S04 (0.708); R24 (0.693); A03 (0.671); R26 (0.661); R25 (0.644)

**Evidence 828** (`text`, artifact 2)
- canonical_text: National Institutional Ranking Framework: Total no. of Consultancy Projects
- supporting_context: Total no. of Consultancy Projects
  - A (canonical_text) top-5: X05 (0.737); R23 (0.718); INT09 (0.709); F08 (0.698); R14 (0.698)
  - B (supporting_context) top-5: R14 (0.660); FIN03 (0.639); A14 (0.639); A22 (0.620); A04 (0.619)

**Evidence 946** (`text`, artifact 3)
- canonical_text: National Institutional Ranking Framework: 898
- supporting_context: 898
  - A (canonical_text) top-5: X05 (0.867); X04 (0.748); X03 (0.739); X01 (0.722); R08 (0.698)
  - B (supporting_context) top-5: X04 (0.589); ESG02 (0.546); I18 (0.541); X05 (0.536); R08 (0.532)

**Evidence 1064** (`text`, artifact 3)
- canonical_text: National Institutional Ranking Framework: placed graduates per
- supporting_context: placed graduates per
  - A (canonical_text) top-5: X05 (0.816); P07 (0.758); X03 (0.745); X04 (0.744); X01 (0.736)
  - B (supporting_context) top-5: P01 (0.787); P03 (0.764); R25 (0.761); P02 (0.739); P07 (0.716)

**Evidence 1182** (`text`, artifact 3)
- canonical_text: National Institutional Ranking Framework: 183339247 (Eighteen Crore Thirty Three Lakh Thirty Nine
- supporting_context: 183339247 (Eighteen Crore Thirty Three Lakh Thirty Nine
  - A (canonical_text) top-5: X05 (0.701); X04 (0.596); X03 (0.550); A27 (0.524); S05 (0.523)
  - B (supporting_context) top-5: A27 (0.451); S05 (0.434); X05 (0.430); A06 (0.430); ESG02 (0.429)

**Evidence 1300** (`text`, artifact 4)
- canonical_text: National Institutional Ranking Framework: Government of India
- supporting_context: Government of India
  - A (canonical_text) top-5: X05 (0.839); X04 (0.740); X03 (0.717); X01 (0.705); F08 (0.698)
  - B (supporting_context) top-5: X05 (0.606); S05 (0.602); X04 (0.547); A27 (0.541); F08 (0.520)

**Evidence 1419** (`text`, artifact 4)
- canonical_text: National Institutional Ranking Framework: graduating in
- supporting_context: graduating in
  - A (canonical_text) top-5: X05 (0.810); X04 (0.740); P18 (0.737); X03 (0.733); S12 (0.732)
  - B (supporting_context) top-5: A24 (0.705); A27 (0.704); P01 (0.696); P02 (0.675); P03 (0.669)

**Evidence 1537** (`text`, artifact 4)
- canonical_text: National Institutional Ranking Framework: 46424349 (Four Crores Sixty Four Lakh Twenty Four
- supporting_context: 46424349 (Four Crores Sixty Four Lakh Twenty Four
  - A (canonical_text) top-5: X05 (0.644); X04 (0.541); X03 (0.512); A27 (0.494); A02 (0.491)
  - B (supporting_context) top-5: A27 (0.469); X05 (0.449); A08 (0.447); FIN05 (0.447); F08 (0.442)

**Evidence 1655** (`text`, artifact 5)
- canonical_text: National Institutional Ranking Framework: Government of India
- supporting_context: Government of India
  - A (canonical_text) top-5: X05 (0.840); X04 (0.742); X03 (0.720); X01 (0.708); F08 (0.699)
  - B (supporting_context) top-5: X05 (0.606); S05 (0.602); X04 (0.547); A27 (0.541); F08 (0.520)

**Evidence 1773** (`text`, artifact 5)
- canonical_text: National Institutional Ranking Framework: No. of students
- supporting_context: No. of students
  - A (canonical_text) top-5: X05 (0.789); S12 (0.722); X11 (0.701); FIN04 (0.701); X04 (0.695)
  - B (supporting_context) top-5: S01 (0.825); I18 (0.752); I08 (0.746); INT02 (0.741); ESG02 (0.739)

**Evidence 1891** (`text`, artifact 5)
- canonical_text: National Institutional Ranking Framework: 2023-24
- supporting_context: 2023-24
  - A (canonical_text) top-5: X05 (0.841); X03 (0.737); X04 (0.731); X01 (0.724); R05 (0.703)
  - B (supporting_context) top-5: ESG02 (0.607); I18 (0.601); A01 (0.582); A02 (0.579); A27 (0.567)

**Evidence 2009** (`text`, artifact 5)
- canonical_text: National Institutional Ranking Framework: Yes, more than 80% of the buildings
- supporting_context: Yes, more than 80% of the buildings
  - A (canonical_text) top-5: X05 (0.636); X04 (0.512); G01 (0.511); INT03 (0.511); I16 (0.504)
  - B (supporting_context) top-5: I02 (0.525); I16 (0.517); ESG09 (0.415); I04 (0.410); I03 (0.409)

**Evidence 2128** (`text`, artifact 6)
- canonical_text: National Institutional Ranking Framework: 1186
- supporting_context: 1186
  - A (canonical_text) top-5: X05 (0.876); X04 (0.773); X03 (0.767); X01 (0.748); R08 (0.729)
  - B (supporting_context) top-5: ESG02 (0.504); X04 (0.493); X14 (0.491); ESG05 (0.474); I18 (0.470)

**Evidence 2246** (`text`, artifact 6)
- canonical_text: National Institutional Ranking Framework: placed
- supporting_context: placed
  - A (canonical_text) top-5: X05 (0.805); X04 (0.724); X03 (0.690); X01 (0.657); R08 (0.648)
  - B (supporting_context) top-5: P03 (0.623); P02 (0.609); X04 (0.594); P01 (0.593); S20 (0.568)

**Evidence 2364** (`text`, artifact 6)
- canonical_text: National Institutional Ranking Framework: 2020-21
- supporting_context: 2020-21
  - A (canonical_text) top-5: X05 (0.844); X03 (0.741); X04 (0.738); X01 (0.723); R08 (0.703)
  - B (supporting_context) top-5: I18 (0.574); ESG02 (0.573); A02 (0.561); X04 (0.558); A01 (0.557)

**Evidence 2482** (`text`, artifact 6)
- canonical_text: National Institutional Ranking Framework: Seven Hundred And Ninety six)
- supporting_context: Seven Hundred And Ninety six)
  - A (canonical_text) top-5: X05 (0.822); X04 (0.714); X03 (0.705); X01 (0.691); F08 (0.681)
  - B (supporting_context) top-5: X04 (0.554); ESG02 (0.542); X05 (0.530); ESG05 (0.519); I18 (0.518)

**Evidence 2600** (`text`, artifact 7)
- canonical_text: National Institutional Ranking Framework: Socially
- supporting_context: Socially
  - A (canonical_text) top-5: X05 (0.792); X04 (0.731); X01 (0.715); F08 (0.714); G01 (0.711)
  - B (supporting_context) top-5: X14 (0.624); S17 (0.619); X04 (0.587); P08 (0.585); A08 (0.579)

**Evidence 2718** (`text`, artifact 7)
- canonical_text: National Institutional Ranking Framework: 1880000(Eighteen
- supporting_context: 1880000(Eighteen
  - A (canonical_text) top-5: X05 (0.778); X04 (0.704); X03 (0.677); X01 (0.650); R08 (0.636)
  - B (supporting_context) top-5: ESG02 (0.430); FIN16 (0.429); P02 (0.420); FIN08 (0.416); P03 (0.408)

**Evidence 2837** (`text`, artifact 7)
- canonical_text: National Institutional Ranking Framework: Hundred Fifty Six Only)
- supporting_context: Hundred Fifty Six Only)
  - A (canonical_text) top-5: X05 (0.838); X04 (0.726); X03 (0.709); X01 (0.681); R08 (0.670)
  - B (supporting_context) top-5: FIN15 (0.600); X04 (0.577); A08 (0.571); P08 (0.567); I17 (0.565)

**Evidence 2955** (`text`, artifact 7)
- canonical_text: National Institutional Ranking Framework: Total Amount Received (Amount in Rupees)
- supporting_context: Total Amount Received (Amount in Rupees)
  - A (canonical_text) top-5: X05 (0.811); X04 (0.719); X03 (0.699); F07 (0.696); R23 (0.683)
  - B (supporting_context) top-5: R06 (0.599); S09 (0.581); FIN12 (0.563); R11 (0.560); ESG02 (0.547)

**Evidence 3074** (`text`, artifact 8)
- canonical_text: National Institutional Ranking Framework: -
- supporting_context: -
  - A (canonical_text) top-5: X05 (0.789); X04 (0.675); X03 (0.646); X01 (0.611); R08 (0.607)
  - B (supporting_context) top-5: X04 (0.582); FIN16 (0.558); X05 (0.557); ESG02 (0.554); R21 (0.549)

**Evidence 3192** (`text`, artifact 8)
- canonical_text: National Institutional Ranking Framework: No. of students
- supporting_context: No. of students
  - A (canonical_text) top-5: X05 (0.789); S12 (0.722); X11 (0.701); FIN04 (0.701); X04 (0.695)
  - B (supporting_context) top-5: S01 (0.825); I18 (0.752); I08 (0.746); INT02 (0.741); ESG02 (0.739)

**Evidence 3310** (`text`, artifact 8)
- canonical_text: National Institutional Ranking Framework: selected for Higher
- supporting_context: selected for Higher
  - A (canonical_text) top-5: X05 (0.800); X04 (0.730); X03 (0.727); X01 (0.701); R08 (0.688)
  - B (supporting_context) top-5: P05 (0.676); S03 (0.651); S12 (0.636); A01 (0.626); A02 (0.625)

**Evidence 3428** (`text`, artifact 8)
- canonical_text: National Institutional Ranking Framework: Seminars/Conferences/Workshops
- supporting_context: Seminars/Conferences/Workshops
  - A (canonical_text) top-5: X05 (0.766); F08 (0.718); X01 (0.706); R23 (0.694); G01 (0.694)
  - B (supporting_context) top-5: P16 (0.672); A08 (0.626); F08 (0.623); A20 (0.609); A07 (0.591)

## 14. KPI sample (20, deterministic, evenly spaced by kpi_code)

### KPI sample -> top-10 evidence (representation A vs B)

**A01** — UG programme count: Number of undergraduate programmes
  - A (canonical_text) top-5: #1680/text (0.742); #1325/text (0.742); #892/text (0.742); #456/text (0.742); #2059/text (0.742)
  - B (supporting_context) top-5: #2564/text (0.775); #442/text (0.775); #878/text (0.775); #2038/text (0.775); #3061/text (0.775)

**A12** — International participation: Programmes with external/international academic participation
  - A (canonical_text) top-5: #1513/text (0.734); #1523/text (0.734); #714/text (0.734); #724/text (0.734); #1875/text (0.734)
  - B (supporting_context) top-5: #1176/text (0.650); #1535/text (0.650); #302/text (0.650); #736/text (0.650); #1897/text (0.650)

**A23** — First-year retention: Students retained after first year
  - A (canonical_text) top-5: #191/text (0.678); #188/text (0.678); #133/text (0.678); #130/text (0.678); #233/text (0.678)
  - B (supporting_context) top-5: #1410/text (0.726); #3171/text (0.726); #233/text (0.726); #1049/text (0.726); #188/text (0.725)

**A35** — Library expenditure/student: Annual library expenditure per student
  - A (canonical_text) top-5: #341/text (0.723); #1215/text (0.723); #1574/text (0.721); #775/text (0.721); #1936/text (0.721)
  - B (supporting_context) top-5: #1176/text (0.761); #1535/text (0.761); #736/text (0.761); #302/text (0.761); #1897/text (0.761)

**ESG10** — Sustainable transport: Students/staff using sustainable transport modes
  - A (canonical_text) top-5: #3527/text (0.730); #3022/text (0.657); #41/text (0.617); #39/text (0.617); #898/text (0.617)
  - B (supporting_context) top-5: #1642/text (0.646); #411/text (0.646); #847/text (0.646); #1287/text (0.646); #2007/text (0.646)

**F06** — Research-active faculty: Faculty with qualifying research output/activity
  - A (canonical_text) top-5: #419/text (0.786); #855/text (0.786); #1295/text (0.786); #1650/text (0.786); #2015/text (0.786)
  - B (supporting_context) top-5: #1296/text (0.699); #1651/text (0.699); #420/text (0.699); #856/text (0.699); #2016/text (0.699)

**F17** — Editorial/professional roles: Faculty serving on major editorial/professional bodies
  - A (canonical_text) top-5: #2015/text (0.689); #1295/text (0.689); #1650/text (0.689); #855/text (0.689); #2541/text (0.689)
  - B (supporting_context) top-5: #1295/text (0.654); #1650/text (0.654); #419/text (0.654); #855/text (0.654); #2015/text (0.654)

**FIN11** — Operating margin: Operating surplus divided by operating revenue
  - A (canonical_text) top-5: #341/text (0.390); #1215/text (0.390); #775/text (0.388); #1574/text (0.388); #1936/text (0.388)
  - B (supporting_context) top-5: #1574/text (0.423); #775/text (0.423); #341/text (0.423); #1215/text (0.423); #1936/text (0.423)

**G06** — Administrative audit: Administrative functions audited periodically
  - A (canonical_text) top-5: #1215/text (0.567); #341/text (0.567); #775/text (0.566); #1574/text (0.566); #1936/text (0.566)
  - B (supporting_context) top-5: #1207/text (0.576); #767/text (0.576); #333/text (0.576); #1566/text (0.576); #1928/text (0.576)

**G18** — AI governance: Responsible AI policy and governance mechanism
  - A (canonical_text) top-5: #3527/text (0.587); #2090/text (0.577); #487/text (0.577); #1356/text (0.577); #1711/text (0.577)
  - B (supporting_context) top-5: #3516/text (0.567); #3533/text (0.540); #1301/text (0.521); #425/text (0.521); #4/text (0.521)

**I11** — Network uptime: Campus network availability
  - A (canonical_text) top-5: #339/text (0.640); #340/text (0.640); #300/text (0.640); #338/text (0.640); #1174/text (0.640)
  - B (supporting_context) top-5: #1287/text (0.629); #1642/text (0.629); #847/text (0.629); #411/text (0.629); #2007/text (0.629)

**I22** — HPC/GPU availability: Institutional access to HPC/GPU resources
  - A (canonical_text) top-5: #340/text (0.681); #300/text (0.681); #338/text (0.681); #339/text (0.681); #1174/text (0.681)
  - B (supporting_context) top-5: #1287/text (0.659); #1642/text (0.659); #847/text (0.659); #411/text (0.659); #2007/text (0.659)

**INT12** — Visa/accommodation support: International students receiving structured arrival/support services
  - A (canonical_text) top-5: #127/text (0.662); #985/text (0.662); #1404/text (0.662); #549/text (0.662); #1759/text (0.662)
  - B (supporting_context) top-5: #1287/text (0.651); #1642/text (0.651); #411/text (0.651); #847/text (0.651); #2007/text (0.651)

**P11** — Employer satisfaction: Employer satisfaction score
  - A (canonical_text) top-5: #3501/text (0.649); #3007/text (0.649); #1425/text (0.627); #627/text (0.627); #570/text (0.627)
  - B (supporting_context) top-5: #3361/text (0.620); #294/text (0.615); #728/text (0.615); #1168/text (0.615); #1527/text (0.615)

**R04** — Q1 publications: Publications in Q1 journals
  - A (canonical_text) top-5: #1141/text (0.712); #887/text (0.712); #3131/text (0.708); #438/text (0.702); #577/text (0.702)
  - B (supporting_context) top-5: #575/text (0.681); #153/text (0.681); #250/text (0.681); #205/text (0.681); #632/text (0.681)

**R15** — Patents filed: Patent applications filed
  - A (canonical_text) top-5: #427/text (0.535); #1303/text (0.532); #863/text (0.532); #6/text (0.532); #1658/text (0.532)
  - B (supporting_context) top-5: #3377/text (0.582); #2883/text (0.582); #508/text (0.570); #87/text (0.570); #101/text (0.570)

**R27** — GPU/HPC capacity: Institutional high-performance computing capacity
  - A (canonical_text) top-5: #1905/text (0.674); #2429/text (0.674); #744/text (0.674); #1543/text (0.674); #310/text (0.673)
  - B (supporting_context) top-5: #1642/text (0.658); #847/text (0.658); #1287/text (0.658); #411/text (0.658); #2007/text (0.658)

**S06** — Other-state students: Students from states other than host state
  - A (canonical_text) top-5: #2589/text (0.648); #467/text (0.648); #1691/text (0.648); #2070/text (0.648); #3086/text (0.648)
  - B (supporting_context) top-5: #923/text (0.717); #1356/text (0.717); #487/text (0.717); #66/text (0.717); #1711/text (0.717)

**S18** — Student awards: National/international student awards
  - A (canonical_text) top-5: #2394/text (0.671); #1508/text (0.671); #1870/text (0.671); #709/text (0.671); #2848/text (0.671)
  - B (supporting_context) top-5: #1331/text (0.651); #39/text (0.650); #460/text (0.650); #41/text (0.650); #462/text (0.650)

**X05** — NIRF rank: NIRF national/category ranking
  - A (canonical_text) top-5: #1439/text (0.918); #1460/text (0.918); #511/text (0.903); #945/text (0.887); #2633/text (0.886)
  - B (supporting_context) top-5: #1298/text (0.870); #422/text (0.870); #1/text (0.870); #858/text (0.870); #1653/text (0.870)

## 16. Observations

See `observations.md` at the repository root for detailed, dated observations from this run. In short: this is a raw semantic-similarity baseline with no correctness guarantee — matches that look wrong are expected and are the point of measuring, not a defect in this phase's implementation.

## 17. Complete result files

- `evidence_to_kpi_A.jsonl`
- `evidence_to_kpi_B.jsonl`
- `kpi_to_evidence_A.jsonl`
- `kpi_to_evidence_B.jsonl`
