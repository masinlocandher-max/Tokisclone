from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("REST_HOST", "0.0.0.0")
    port = int(os.getenv("REST_PORT", "8080"))
    uvicorn.run("rest_api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
