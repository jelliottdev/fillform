# FillForm — Brutally Honest Capability Scorecard

Percent-complete estimates by capability area, relative to "perfect FillForm"
as defined in the maturity discussion.  Each area has a score, an honest
explanation of what is and is not working, and the specific gap to close next.

Scores are engineering estimates of working, tested, production-ready coverage.
Architecture and interfaces that exist but are untested on real forms count at
most 60% of full credit.

---

## Summary Table

| Capability | Score | Status |
|---|---|---|
| PDF ingestion & field extraction | 88% | Solid; scanned/encrypted gaps remain |
| Field semantic mapping | 72% | Vision pipeline complete; multi-page context missing |
| Fill engine | 80% | AcroForm reliable; repeating sections not implemented |
| Post-fill verification | 75% | 4 checks wired; cross-form and section-level missing |
| Arithmetic & cross-field validation | 58% | Rules implemented; not auto-run; no cross-form |
| Visual QA | 62% | Two check modes exist; untested on ugly real forms |
| Schema versioning & migration | 52% | Diff + migration plan implemented; not MCP-exposed |
| Repeating structures | 5% | Schema concept missing; fill engine not built for it |
| Cross-form packet validation | 10% | PacketValidator concept exists; not integrated |
| Fill report & review artifacts | 68% | Markdown + JSON report complete; no review UX loop |
| Quality measurement | 60% | Thresholds defined; no test corpus; not in CI |
| MCP / API surface | 55% | 15 tools; new modules not yet exposed |
| Bankruptcy-specific form coverage | 18% | No tested form corpus; no court-specific rules |
| Platform infrastructure | 8% | No persistence backend, auth, bulk, or webhooks |

**Overall toward "perfect FillForm": ~58%**

---

## Detailed Breakdown

### 1. PDF Ingestion & Field Extraction — 88%

**What works**
- Dual-parser (PyMuPDF + pypdf fallback) with graceful degradation
- AcroForm field widget extraction: bbox, page, field type, field name
- Encrypted PDF detection with clear error
- SHA-256 fingerprinting for session matching
- FXXX sequential alias assignment with reading-order sort

**What is missing**
- Scanned (image-only) PDFs — detected and rejected but not handled; the gap for forms that were printed and rescanned
- Forms with duplicate field names across pages — silent data-loss risk
- Signature field value handling — detected as "Sig" type but not filled or validated
- Incremental-update PDFs (some court forms save as append-only) — untested behaviour

**To close this gap**
Detect duplicate field names during extraction and surface a warning.  Add a
scanned-PDF flag that routes to an overlay path instead of silently failing.

---

### 2. Field Semantic Mapping — 72%

**What works**
- Multi-pass Claude vision analysis (2 passes per page)
- Annotated PDF rendering (orange FXXX labels) for vision context
- Nearby-text label inference as geometry-only fallback
- CanonicalSchema serialisation to/from disk
- Semantic fields: label, context, expected_value_type, expected_format, is_required, section

**What is missing**
- **Multi-page context** — each page analysed independently; no cross-page grouping or continuation detection
- **Table / repeating section detection** — vision sees rows but produces flat independent fields
- **Section-level semantic rules** — "at least one field in this group must be non-empty"
- **Confidence propagation** — low-confidence mappings not flagged for human review in the schema itself
- **Group relationships** — radio groups and mutually exclusive sets not modelled as a schema concept

**To close this gap**
Add a `FieldGroup` concept to `CanonicalSchema` so the vision mapper can emit
groups alongside individual fields.  Run a cross-page merge pass after
per-page vision analysis.

---

### 3. Fill Engine — 80%

**What works**
- Text (Tx) fields: correct value coercion and write
- Single checkbox (Btn): resolves on-state via `button_states()`
- Radio/checkbox groups (multiple widgets sharing a name): select one, deselect others
- Choice (Ch) / dropdown fields: string value written directly
- Alias → field_name resolution via schema
- Deterministic FillWriteAction log with checksums
- Configurable output path
- Before/after change tracking per field

**What is missing**
- **Repeating sections** — no concept of "fill rows 1–N from an array"; each field is treated independently
- **Continuation pages** — if a creditor list overflows onto a second page, there is no orchestration
- **Overlay mode** — non-AcroForm digital PDFs cannot be filled at all
- **Signature fields** — placeholder-only; actual signature insertion not implemented
- **Multi-form packet fill** — each form filled independently; no shared-value propagation

**To close this gap**
Add a `RepeatingSection` schema concept and a fill-engine pass that iterates
over array payloads to fill repeated row groups.

---

### 4. Post-fill Verification — 75%

**What works**
- Completeness: all required fields present and non-empty
- PDF readback: stored widget values compared to intended values
- Format validation: dates, numbers, SSN, ZIP
- Field constraints: min/max, enum, required_if, exclusive_with, pattern, length

**What is missing**
- **Arithmetic not auto-run** — `ArithmeticValidator` exists but must be called manually; not wired into `VerificationEngine.verify()`
- **Cross-form consistency** — no validation that the same value appears correctly in two different forms
- **Section-level completeness** — "at least one of these fields must be filled"
- **Derived field evaluation** — `derived_from` constraint is documented but not computed

**To close this gap**
Wire `ArithmeticValidator` into `VerificationEngine.verify()` when a schema
has arithmetic constraints.  Add a cross-form check to `PacketValidator`.

---

### 5. Arithmetic & Cross-field Validation — 58%

**What works**
- `sum_of`, `diff_of`, `equals_field`, `percent_of` rules
- Per-check `ArithmeticCheckResult` with expected/actual/delta/tolerance
- `ArithmeticReport.as_validation_issues()` for integration with `VerificationReport`
- `ArithmeticReport.summary()` human-readable output

**What is missing**
- **Not automatically triggered** — caller must instantiate `ArithmeticValidator` manually; `VerificationEngine` does not call it
- **No bankruptcy-specific constraints pre-built** — Schedule I/J totals, means-test calculations not yet encoded in any schema
- **No cross-form arithmetic** — `equals_field` is intra-form only; cross-form total consistency not modelled
- **`derived_from` not evaluated** — marked informational only; no computation

**To close this gap**
Wire `ArithmeticValidator` into the main verification pipeline as check #5.
Encode Schedule I/J total constraints into the official form schemas.

---

### 6. Visual QA — 62%

**What works**
- `check()` — text-extraction based: possibly_empty, possible_overflow, checkbox_mismatch
- `render_check()` — pixel-level: near-white region detection, right-edge overflow, pixel checkbox state
- `VisualQAReport` with per-field results and `.summary()`
- Integrated into `FillReport.review_queue()` as source

**What is missing**
- **Untested on real court forms** — heuristic thresholds (97% white → empty, 80% white → checkbox checked) not calibrated against actual bankruptcy PDFs
- **Appearance stream validation** — checkbox marks can be missing even when stored value is correct (AP stream issue); not deeply checked
- **Multi-reader compatibility** — form may look fine in PyMuPDF rendering but broken in Acrobat or Preview
- **Clipped label text** — label/header text clipping not checked, only field value region
- **Performance** — `render_check()` re-renders every page; no page-level caching

**To close this gap**
Run `render_check()` against 50+ real bankruptcy PDFs and tune whiteness
thresholds from actual data.  Add Acrobat-compatible rendering option.

---

### 7. Schema Versioning & Migration — 52%

**What works**
- `diff_schemas()` with three match strategies: exact name → label fuzzy → position heuristic
- `migration_plan()` with carry-forward/re-review/remove/add-required actions
- `FieldDiff` with attribute-level `FieldChange` records
- `SchemaDiff.summary()` human-readable diff

**What is missing**
- **Not exposed as MCP tool** — exists as a Python module; agents cannot call it
- **Automated form version detection** — no fingerprint-to-version lookup; callers must know which version they have
- **Version history** — registry stores only one schema per (family, version); no timestamp history
- **Alias reassignment handling** — if FXXX aliases change between versions, migration plan may be misleading
- **Similarity threshold tuning** — label-fuzzy match uses 0.75 as cut-off; not validated against real form updates

**To close this gap**
Add `diff_schemas` and `migration_plan` as MCP tools.  Add form-fingerprint-
to-version lookup in the registry so agents can auto-detect which version they're working with.

---

### 8. Repeating Structures — 5%

**What works**
- Nothing production-ready
- `CanonicalField` can represent individual fields in a table row (they just look like any other field)

**What is missing**
- **`RepeatingSection` schema concept** — no way to declare "fields F010–F015 repeat N times"
- **Array payload support** — fill engine expects a flat `{alias: value}` dict; no `{section: [{...}, {...}]}` concept
- **Row-group fill orchestration** — no logic to copy a row template and fill multiple instances
- **Continuation page detection** — vision mapper does not detect "this page is a continuation of the previous form"
- **Dynamic attachment generation** — some bankruptcy forms require separate creditor matrix files

**To close this gap**
Add `RepeatingSection` to `contracts.py`.  Add array-payload support to
`FillPayload`.  Implement a row-expansion pass in `FillEngine`.  This is the
largest single remaining functional gap.

---

### 9. Cross-form Packet Validation — 10%

**What works**
- `PacketValidator` concept in `packet.py` (debtor identity, case number, total consistency across forms)
- `PacketReport` with cross-form issues

**What is missing**
- **Not integrated into any workflow** — `PacketValidator` exists but is not called by `mcp_server.py` or any MCP tool
- **No packet schema** — no formal definition of "a Chapter 7 packet requires these 11 forms"
- **No cross-form arithmetic** — Schedule I totals must match means test; no rule to express this yet
- **No packet completeness check** — no check that all required forms are present
- **Disclosure cross-reference** — SOFA disclosures not checked against petition representations

**To close this gap**
Define a `PacketSchema` (which forms are required for Chapter 7/13/11).  Wire
`PacketValidator` into a packet-level MCP tool.  Add cross-form arithmetic rules.

---

### 10. Fill Report & Review Artifacts — 68%

**What works**
- `FillReport.build()` aggregates all four artifact types
- `review_queue()` with priority 1/2/3 ranking and suggested attorney actions
- `to_markdown()` with filing-readiness banner, summary table, field log, changed-fields diff
- `to_dict()` machine-readable JSON

**What is missing**
- **No approve/edit/re-run loop** — the report is read-only; no way to record attorney corrections and re-trigger a targeted re-fill
- **No side-by-side evidence** — report does not show where the value came from (intake, document extraction, manual entry)
- **No diff between runs** — no way to compare two fill runs to see what changed
- **No saved decisions** — attorney corrections are not persisted back to the schema as annotations
- **No UI integration** — the markdown report exists but has no rendering target

**To close this gap**
Add a `FillDecision` dataclass for recording corrections.  Add
`FillReport.diff(other_report)` for run comparison.  This is primarily a
product layer gap, not an engine gap.

---

### 11. Quality Measurement — 60%

**What works**
- Five quality dimensions with named thresholds
- `QualityReport.from_artifacts()` computes scores from real artifacts
- `meets_legal_grade_threshold` flag
- `summary()` and `to_dict()` output

**What is missing**
- **No test corpus** — thresholds exist but have never been measured against a real form corpus
- **No regression tracking** — `QualityReport` is ephemeral; scores not persisted over time
- **Not integrated into CI** — no automated run to detect quality regressions
- **Threshold calibration** — 97% fill accuracy and verification match targets are reasonable guesses; need data to validate

**To close this gap**
Build a test corpus of 20–30 bankruptcy PDFs with known correct fills.  Run
`QualityReport` against every fill in the corpus.  Tune thresholds from data.
Store quality results in the schema registry alongside the schema.

---

### 12. MCP / API Surface — 55%

**What works**
- 15 MCP tools covering the original fill pipeline
- HTTP/SSE transport via Vercel
- Session management with fingerprint validation
- `analyze_form`, `fill_pdf_form`, `validate_form`, `complete_form`, etc.

**What is missing**
- `arithmetic_validate` — not exposed
- `visual_qa_check` and `visual_qa_render_check` — not exposed
- `quality_report` — not exposed
- `schema_diff` and `migration_plan` — not exposed
- `packet_validate` — not exposed
- No versioned HTTP API separate from MCP
- No bulk-fill endpoint
- No auth or rate limiting

**To close this gap**
Add MCP tools for all new modules.  This is the highest-leverage immediate
action: the modules are built and tested, they just need tool wrappers.

---

### 13. Bankruptcy-specific Form Coverage — 18%

**What works**
- Generic AcroForm handling that should work on any well-formed PDF
- Demo value generation for common field types (name, date, income, etc.)
- Means-test-aware demo value heuristics (partial)

**What is missing**
- No tested schema for B-101 (Voluntary Petition)
- No tested schema for B-106A/B (Schedules A/B)
- No tested schema for B-106E/F (Schedules E/F — creditor lists)
- No tested schema for B-106I/J (Schedules I/J — income/expenses)
- No tested schema for B-107 (Statement of Financial Affairs)
- No tested schema for B-122A-1/2 (Means Test)
- No district-specific local forms
- No court-specific field naming quirks catalogued
- No means-test arithmetic schema constraints pre-encoded

**To close this gap**
This is the most important "execution" work.  Download and map all Official
Bankruptcy Forms.  Run QualityReport on each.  Store verified schemas in the
registry.  Encode arithmetic constraints for the forms with totals.

---

### 14. Platform Infrastructure — 8%

**What works**
- Vercel deployment configuration
- In-process session management (LRU eviction)
- Schema JSON persistence to disk

**What is missing**
- No database backend (Postgres, Supabase, etc.)
- No auth or multi-tenancy
- No bulk-fill job queue
- No webhook / event stream for fill completion
- No audit log store (fill logs are ephemeral)
- No API versioning
- No rate limiting or abuse protection
- No monitoring or alerting

**To close this gap**
This is Stage 4 work.  Do not build it until Stage 3 is solid on real forms.
The correct order is: test corpus → reliability hardening → platform layer.

---

## Honest Overall Assessment

**Current state: ~58% toward perfect**

The engine is architecturally sound and covers all the major concepts.  The
code is clean, the contracts are stable, and the module boundaries are correct.
What is missing is mostly execution: testing on ugly real forms, wiring modules
together, and building the bankruptcy-specific schema library.

The most impactful next actions, in order:

| Priority | Action | Impact |
|----------|--------|--------|
| 1 | Wire ArithmeticValidator into VerificationEngine | Closes biggest silent-error gap |
| 2 | Expose new modules as MCP tools | Makes existing work accessible |
| 3 | Build B-101/106/107/122A form corpus and run QualityReport | Validates everything |
| 4 | Implement RepeatingSection schema and fill | Enables real creditor lists |
| 5 | Wire PacketValidator into a packet workflow | Enables cross-form trust |
| 6 | Calibrate visual QA thresholds from real data | Reduces false positives |
| 7 | Add FillReport diff between runs | Enables review UX loop |

**The gap between ~58% and "perfect" is mostly execution, not invention.**

The architecture is at 85%+ of what it needs to be.  The remaining work is:
running it against real forms, measuring failures, fixing them, and repeating
until QualityReport consistently returns `meets_legal_grade_threshold = True`
on the full official form corpus.

That is how "promising engine" becomes "trusted legal-grade infrastructure."
