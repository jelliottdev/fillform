"""Entry point: python -m fillform.mcp  or  python -m fillform"""

import asyncio
from .mcp import main

asyncio.run(main())
