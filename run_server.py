from __future__ import annotations

import os

from server import mcp


def main() -> None:
    mcp.settings.host = os.getenv("HOST", "0.0.0.0")
    mcp.settings.port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
