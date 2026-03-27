# FillForm Maturity Checklist

A staged framework for evaluating and advancing FillForm toward legal-grade
form infrastructure.  Each stage has a clear capability bar and a concrete set
of criteria.  The stages are cumulative — a higher stage assumes everything
below it is working reliably.

---

## Stage 1 — Prototype
> "I can fill a known PDF with known data and the fields are probably right."

The system exists and can write values into a PDF.  Results are not reliably
verifiable.  Suitable for demos and manual review only.

### Criteria

- [ ] PDF ingestion handles common AcroForm files without crashing
- [ ] Field extraction assigns aliases and returns a usable alias map
- [ ] Fill engine writes text field values
- [ ] Fill engine handles single checkboxes (on/off)
- [ ] Output PDF opens without errors in a standard reader
- [ ] At least one end-to-end fill can be demonstrated manually

### Current status: **COMPLETE** ✓

---

## Stage 2 — Reliable Engine
> "I can fill a form, verify that the fill happened correctly, and explain what failed."

The system is deterministic and self-checking.  An attorney or paralegal can
trust the output of a fill operation because they can see proof of what happened.

### Criteria

**Fill reliability**
- [x] Text fields fill correctly
- [x] Single checkboxes fill correctly (resolves on-state via `button_states()`)
- [x] Radio button groups fill correctly (select one, deselect all others)
- [x] Choice (dropdown) fields fill correctly
- [x] Alias → field_name resolution via canonical schema
- [x] Deterministic write-action log (FillWriteAction with checksums)
- [x] Output path is configurable (not hardcoded to temp)

**Verification**
- [x] Completeness check: all required fields present and non-empty
- [x] PDF readback: stored widget values match intended values after fill
- [x] Format checks: date, number, SSN, ZIP code patterns
- [x] Structured VerificationReport with per-check status and field-level issues

**Schema**
- [x] CanonicalSchema persists to disk (not only in memory)
- [x] Schema survives MCP process restarts
- [x] Schema is reusable for repeat fills of the same form

**Auditability**
- [x] Fill log maps every input key to its outcome (ok / missing / error)
- [x] Changed-field log records before/after values
- [x] Verification report is serialisable and can be stored

### Current status: **COMPLETE** ✓

---

## Stage 3 — Legal-Grade Engine
> "I trust this more than a rushed human operator."

The system catches the mistakes humans miss and can explain every decision.
Suitable for use in a real legal workflow under attorney supervision.

### Criteria

**Visual QA**
- [x] Text fields that appear visually empty are flagged (even if stored value is set)
- [x] Likely text overflow is detected and flagged per-field
- [x] Checkbox visual state is verified against stored state
- [ ] Pixel-level rendering checks (overflow clipping, hidden text, alignment)
- [ ] Form renders correctly in at least two major PDF readers (Acrobat, Preview)

**Richer constraints**
- [x] FieldConstraint type in schema: `min_value`, `max_value`, `enum`
- [x] FieldConstraint: `required_if` (conditional required)
- [x] FieldConstraint: `exclusive_with` (mutually exclusive checkboxes)
- [x] FieldConstraint: `pattern`, `min_length`, `max_length`
- [x] VerificationEngine evaluates all constraints in `_constraint_check`
- [ ] `derived_from` constraint computes and validates calculated fields
- [ ] Section-level completeness rules ("at least one in this group must be filled")
- [ ] Cross-field arithmetic validation (Schedule J totals match line items)

**Schema versioning**
- [x] `SchemaDiff` detects added / removed / changed fields between versions
- [x] `migration_plan()` produces ranked action list (carry-forward / re-review / remove / add)
- [x] Match strategies: exact name → label fuzzy → position heuristic
- [ ] Version diff is exposed as an MCP tool
- [ ] Registry `put()` stores version history, not just latest
- [ ] Alias reassignment is handled gracefully in migration plans

**Fill reliability (hard cases)**
- [ ] Multi-page forms fill correctly across all pages
- [ ] Repeating sections / continuation pages are handled
- [ ] Forms with duplicate field names are handled without silent data loss
- [ ] Encrypted PDFs are detected and rejected with a clear error
- [ ] Scanned (image-only) PDFs are detected and rejected with overlay guidance

**Bankruptcy-specific**
- [ ] B-101, B-106, B-107, B-122A-1/2 forms fill reliably end-to-end
- [ ] Means test arithmetic is validated
- [ ] SOFA recent-transactions section handles repeating rows
- [ ] Local district forms (e.g. N.D. Ill., S.D.N.Y.) are catalogued and tested

### Current status: **~50% complete** — visual QA and constraints are in place;
pixel-level QA, cross-field arithmetic, and court-specific form testing remain.

---

## Stage 4 — Platform-Ready Engine
> "This is the trusted form engine behind the whole bankruptcy copilot."

The system is a first-class infrastructure component: API-first, multi-tenant,
auditable, and embeddable in a larger case-assembly workflow.

### Criteria

**API and integration**
- [ ] Stable versioned HTTP API (not just MCP tools)
- [ ] Schema-first I/O — callers pass structured JSON, not free-form values
- [ ] Deterministic bulk fill: 50 petitions/hour without manual intervention
- [ ] Webhook or event stream for fill completion and review queue updates
- [ ] Auth and multi-tenancy (per-firm schema isolation)

**Review UX**
- [ ] Field-by-field review interface (alias, value, confidence, source)
- [ ] Write diff between two fill runs (what changed and why)
- [ ] "What failed" explanations are human-readable, not just error codes
- [ ] One-click correction triggers a targeted re-fill of changed fields only
- [ ] Attorney-approved corrections are persisted as schema annotations

**Audit and trust**
- [ ] Immutable fill log per matter (timestamped, tamper-evident)
- [ ] Chain of custody: intake → extraction → mapping → fill → verification → review
- [ ] Diff report between intended values and final filed document
- [ ] Version history for every schema family

**Schema ecosystem**
- [ ] Schema library for all 50 Official Bankruptcy Forms
- [ ] Schema library for major district-specific local forms
- [ ] Automated form-version detection (fingerprint → schema lookup)
- [ ] Schema update notifications when court revisions are published

**Reliability targets**
- [ ] Fill accuracy ≥ 97% on a canonical test suite of real bankruptcy PDFs
- [ ] Zero silent failures (every error is surfaced, nothing is swallowed)
- [ ] p95 fill time < 3 seconds for a standard Chapter 7 petition page
- [ ] Schema analysis (new form) completes in < 2 minutes end-to-end

### Current status: **Not started** — architecture is heading in the right
direction; implementation requires persistent infrastructure and workflow integration.

---

## How to use this checklist

**During development** — work through Stage 3 criteria before building Stage 4
infrastructure.  A perfect API is worthless if the fill engine is unreliable.

**During testing** — every unchecked item in Stage 2 or Stage 3 is a potential
trust gap.  Prioritise completing those before adding new features.

**During product review** — share this document with attorneys reviewing the
system.  The stage framing helps non-technical stakeholders understand what
"production-ready" actually means for a legal product.

**During form expansion** — for each new form family, check off the
bankruptcy-specific items in Stage 3 before shipping that form to users.

---

## Honest assessment (as of this revision)

| Stage | Status |
|-------|--------|
| 1 — Prototype | **Complete** |
| 2 — Reliable Engine | **Complete** |
| 3 — Legal-Grade Engine | **~50%** — visual QA + constraints done; hard cases + court forms TBD |
| 4 — Platform-Ready Engine | **Not started** |

The system is currently a credible **v1 form engine**.  It is not yet a
**legal-grade engine** because pixel-level visual QA, cross-field arithmetic
validation, and real-world court form testing are not done.  It is not yet
**platform-ready** because it has no persistent API, review UX, or audit trail
beyond the fill log.

The path to "trusted legal infrastructure" is clear and the architecture
supports it.  The remaining work is execution.
