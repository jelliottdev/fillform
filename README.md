# fillform

Architecture skeleton for a form-compiler pipeline:

- ingest PDF metadata
- map to canonical schema
- deterministically fill outputs
- verify result quality
- expose tool-facing API surface

## Layout

- `docs/architecture.md` - system design and module map
- `src/fillform/contracts.py` - core data contracts
- `src/fillform/ingest.py` - ingestion service scaffold
- `src/fillform/mapper.py` - semantic mapping scaffold
- `src/fillform/schema_registry.py` - schema memory scaffold
- `src/fillform/fill_engine.py` - deterministic fill scaffold
- `src/fillform/verify.py` - verification scaffold
- `src/fillform/mcp_server.py` - MCP/API surface scaffold

## Bankruptcy forms sync API

This repo now includes a Starlette app for mirroring official US Courts bankruptcy forms with conservative crawl behavior.

- Module: `fillform.bankruptcy_api:app`
- Unified library entrypoint: `fillform.bankruptcy_tool.BankruptcyFormsTool`
- Endpoint: `POST /bankruptcy-forms/sync`
- Vercel UI routes:
  - `/` MCP setup + tutorial + live analytics dashboard
  - `/bankruptcy-analytics.json` analytics data feed (`?refresh=1` to force a live refresh)
  - Analytics now includes richer fields (`analytics.chapter_counts`, `analytics.schedule_records`, `analytics.doc_type_counts`, `analytics.unique_page_count`)
- Behavior: reads the bankruptcy index, uses the US Courts sitemap to skip unchanged form pages, downloads PDFs, stores a manifest snapshot, and returns a diff (`added`, `removed`, `changed`) versus the previous sync.
- Each form page can contribute multiple PDFs (e.g., schedule form + related instruction PDF); the manifest stores each discovered PDF as a separate record.
- When `download_pdfs=false`, the syncer still probes PDF headers (`ETag` / `Last-Modified` when available) to improve change detection without full file downloads.
- Anti-bot strategy: conservative User-Agent, request throttling, robots-aware crawl delay support, retry-with-backoff for transient errors, conditional GET caching, and sitemap-assisted incremental fetches.
- Optional request body controls include `min_request_interval_seconds`, `download_pdfs`, and `max_form_pages` (useful for low-impact staged rollouts).

Example run:

```bash
PYTHONPATH=src uvicorn fillform.bankruptcy_api:app --host 0.0.0.0 --port 8080
curl -sS -X POST http://localhost:8080/bankruptcy-forms/sync | jq
```
