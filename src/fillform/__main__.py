"""Entry point: python -m fillform.mcp  or  python -m fillform

stdio (default):
    python -m fillform.mcp

HTTP/SSE (URL mode):
    python -m fillform.mcp --http
    python -m fillform.mcp --http --port 9000
    python -m fillform.mcp --http --host 0.0.0.0 --port 8000
"""

from .mcp import _cli

_cli()
