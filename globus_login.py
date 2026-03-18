"""Two-phase Globus login script.

Usage:
  python globus_login.py get-url          # Phase 1: prints auth URL, saves PKCE state
  python globus_login.py exchange <code>  # Phase 2: exchanges code for tokens
"""

import json
import sys
import pickle
from pathlib import Path

CLIENT_ID = "7df9d534-fb19-4d79-8e83-642f1cdcf081"
TOKEN_FILE = Path.home() / ".globus" / "smartscope_tokens.json"
STATE_FILE = Path("/tmp/globus_pkce_state.pkl")
TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"


def get_url():
    from globus_sdk import NativeAppAuthClient

    auth_client = NativeAppAuthClient(CLIENT_ID)
    auth_client.oauth2_start_flow(
        requested_scopes=[TRANSFER_SCOPE],
        refresh_tokens=True,
    )

    url = auth_client.oauth2_get_authorize_url()

    # Save the client state (contains PKCE code_verifier)
    STATE_FILE.write_bytes(pickle.dumps(auth_client))

    print(f"\nVisit this URL and login:\n\n  {url}\n")
    print(f"Then run: python globus_login.py exchange <AUTH_CODE>\n")


def exchange(auth_code):
    from globus_sdk import NativeAppAuthClient

    if not STATE_FILE.exists():
        print("Error: Run 'python globus_login.py get-url' first.")
        sys.exit(1)

    auth_client = pickle.loads(STATE_FILE.read_bytes())
    token_response = auth_client.oauth2_exchange_code_for_tokens(auth_code)
    tokens = token_response.by_resource_server

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(0o600)

    STATE_FILE.unlink()
    print(f"Tokens saved to {TOKEN_FILE}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "get-url":
        get_url()
    elif cmd == "exchange" and len(sys.argv) > 2:
        exchange(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
