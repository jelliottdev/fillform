# FillForm Architecture: Form Compiler

## Product Thesis

Treat form filling as a **compiler pipeline**:

1. Any form in
2. Canonical schema out
3. Agent fills canonical schema
4. Deterministic writer produces output PDF
5. Verifier checks output fidelity

The core guardrail is that LLMs never write directly to raw PDFs. They reason over schema; the engine writes deterministically.

---

## Core Services

### 1) Ingestion Service
- Input: PDF, optional payload, optional natural-language instructions, optional source docs.
- Output: `DocumentPackage` metadata.
- Duties:
  - hash files
  - detect PDF type (AcroForm/XFA/digital/scanned)
  - detect encryption/password
  - detect native text + form fields

### 2) PDF Structure Service
- Extract hard facts from PDF:
  - widgets, field names/types, coordinates
  - text layer
  - drawing primitives (lines/boxes)
  - page geometry
- Output: `StructuralRepresentation`

### 3) Vision Preparation Service
- Render pages and generate visual context packs:
  - full pages
  - section crops
  - field-region crops
  - boundary context strips across page breaks
- Output: `VisualRepresentation`

### 4) Document Graph Service
- Build graph over field + layout entities.
- Node examples: fields, headings, tables, text blocks, boxes.
- Edge examples: left-of, above, same-row, inside-section, continuation.
- Output: `DocumentGraph`

### 5) Semantic Mapping Engine
- Maps raw fields into canonical field definitions.
- Produces:
  - canonical names
  - types, requiredness
  - confidence + evidence
  - validators
- Output: `CanonicalSchema`

### 6) Fill Engine
- Deterministic write stage (no probabilistic writes).
- Modes:
  - AcroForm
  - Overlay
  - Hybrid
  - Portal automation (for constrained viewer flows)
- Output: interactive + flattened PDFs and fill logs.

### 7) Verification Engine
- Mandatory post-fill checks:
  - expected value visible
  - required fields present
  - no clipping / overlap / wrong-row placement
  - checkbox/radio correctness
- Output: `VerificationReport` (`pass` / `fail` / `abstain`)

### 8) API / MCP Layer
- Tool-facing interface:
  - `upload_form`
  - `analyze_form`
  - `get_schema`
  - `fill_form`
  - `verify_form`
  - `export_form`

---

## Runtime Paths

### Known Form Family (fast path)
1. Upload
2. Match known family/version/hash/fingerprint
3. Load schema
4. Validate payload
5. Deterministic fill
6. Verify
7. Return output

### Unseen Form (slow path)
1. Upload
2. Extract structure + visuals + graph
3. Propose schema
4. Fill draft
5. Verify
6. If pass, store schema for reuse; if fail, retry then abstain

---

## MVP Delivery Plan

### v1
- Interactive PDFs only
- One vertical
- Schema registry + deterministic fill + verification

### v2
- Digital non-interactive PDFs (overlay mode)

### v3
- Scanned forms + stronger OCR and cross-page reasoning

---

## Proposed Module Map

- `src/fillform/contracts.py` → typed data contracts
- `src/fillform/ingest.py` → ingestion + type detection
- `src/fillform/mapper.py` → semantic mapping orchestration
- `src/fillform/schema_registry.py` → family/version schema memory
- `src/fillform/fill_engine.py` → deterministic write modes
- `src/fillform/verify.py` → verification engine
- `src/fillform/mcp_server.py` → API/MCP surface
