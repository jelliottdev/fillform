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
