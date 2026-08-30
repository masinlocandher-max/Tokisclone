from __future__ import annotations

import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    client_secret = Path(os.getenv("GOOGLE_CLIENT_SECRET_FILE", "client_secret.json"))
    token_file = Path(os.getenv("GOOGLE_DRIVE_TOKEN_FILE", "token.json"))

    if not client_secret.exists():
        raise SystemExit(
            f"Missing {client_secret}. Download an OAuth Desktop client JSON from Google Cloud "
            "and place it here, or set GOOGLE_CLIENT_SECRET_FILE."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")

    print(f"Saved OAuth token to {token_file}")
    print("Keep this file private. Never commit it to GitHub.")
    print("For deployment, store its JSON contents in the GOOGLE_DRIVE_TOKEN_JSON secret.")


if __name__ == "__main__":
    main()
