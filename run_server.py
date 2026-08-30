from __future__ import annotations

import os

from mcp.server.transport_security import TransportSecuritySettings

from server import mcp


def _csv_env(name: str) -> list[str]:
    return [
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    ]


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    allowed_hosts = _csv_env("MCP_ALLOWED_HOSTS") or [
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    ]
    allowed_origins = _csv_env("MCP_ALLOWED_ORIGINS") or [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]

    security = TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )


if __name__ == "__main__":
    main()
