#!/usr/bin/env python3
"""SmartScope Globus Pipeline Setup.

One-stop script to authenticate with Globus, deploy the preprocessing
flow, and register the compute function. Run inside the SmartScope
container or anywhere with globus-sdk installed.

Usage:
    python setup_globus.py                # Interactive: auth + deploy + register
    python setup_globus.py auth           # Just authenticate
    python setup_globus.py deploy-flow    # Just deploy/update the flow
    python setup_globus.py register-func  # Just register the compute function
    python setup_globus.py status         # Show current config
"""

import json
import sys
from pathlib import Path

# ---------- Defaults ----------

CLIENT_ID = "7df9d534-fb19-4d79-8e83-642f1cdcf081"
TOKEN_FILE = Path("/opt/config/smartscope_tokens.json")
FLOW_DEFINITION_FILE = Path(__file__).resolve().parent.parent / \
    "Smartscope" / "core" / "pipelines" / "globus_flow_definition.json"

# All scopes needed for the full pipeline
SCOPES = [
    "urn:globus:auth:scope:transfer.api.globus.org:all",
    "https://auth.globus.org/scopes/eec9b274-0c81-4334-bdc2-54e90e689b9a/manage_flows",
    "https://auth.globus.org/scopes/eec9b274-0c81-4334-bdc2-54e90e689b9a/run_manage",
    "https://auth.globus.org/scopes/eec9b274-0c81-4334-bdc2-54e90e689b9a/view_flows",
    "https://auth.globus.org/scopes/facd7ccc-c5f4-42aa-916b-a0e270e2c2a9/all",
]

# ---------- Token helpers ----------

def _load_tokens():
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return {}

def _save_tokens(tokens):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(0o600)

# ---------- Auth ----------

def do_auth():
    from globus_sdk import NativeAppAuthClient

    print("\n=== Globus Authentication ===\n")

    auth_client = NativeAppAuthClient(CLIENT_ID)
    auth_client.oauth2_start_flow(
        requested_scopes=SCOPES,
        refresh_tokens=True,
    )

    url = auth_client.oauth2_get_authorize_url()
    print(f"Visit this URL and log in:\n\n  {url}\n")
    code = input("Paste the authorization code here: ").strip()

    token_response = auth_client.oauth2_exchange_code_for_tokens(code)

    # Merge into existing tokens (preserves funcx_service etc.)
    existing = _load_tokens()
    for rs, data in token_response.by_resource_server.items():
        existing[rs] = dict(data)
    _save_tokens(existing)

    print(f"\nTokens saved to {TOKEN_FILE}")
    for rs in token_response.by_resource_server:
        print(f"  - {rs}")

    return existing

# ---------- Deploy Flow ----------

def do_deploy_flow(tokens=None):
    from globus_sdk import NativeAppAuthClient, FlowsClient, RefreshTokenAuthorizer

    print("\n=== Deploy Globus Flow ===\n")

    if tokens is None:
        tokens = _load_tokens()

    if "flows.globus.org" not in tokens:
        print("Error: No flows token. Run 'setup_globus.py auth' first.")
        return None

    auth_client = NativeAppAuthClient(CLIENT_ID)
    t = tokens["flows.globus.org"]
    authorizer = RefreshTokenAuthorizer(
        t["refresh_token"], auth_client,
        access_token=t["access_token"],
        expires_at=t["expires_at_seconds"],
    )
    fc = FlowsClient(authorizer=authorizer)

    definition = json.loads(FLOW_DEFINITION_FILE.read_text())

    # Check for existing SmartScope flows
    existing_flows = []
    for flow in fc.list_flows():
        if "SmartScope" in flow.get("title", ""):
            existing_flows.append(flow)

    if existing_flows:
        print("Existing SmartScope flows found:")
        for i, flow in enumerate(existing_flows):
            print(f"  [{i+1}] {flow['title']}  ({flow['id']})")
        print(f"  [N] Deploy a new flow")

        choice = input("\nUpdate existing or deploy new? ").strip()
        if choice.upper() == "N":
            title = input("Flow title [SmartScope Globus Preprocessing]: ").strip()
            title = title or "SmartScope Globus Preprocessing"
            result = fc.create_flow(title=title, definition=definition)
            flow_id = result["id"]
            print(f"\nNew flow deployed: {flow_id}")
        else:
            idx = int(choice) - 1
            flow_id = existing_flows[idx]["id"]
            fc.update_flow(flow_id, definition=definition)
            print(f"\nFlow updated: {flow_id}")
    else:
        title = input("Flow title [SmartScope Globus Preprocessing]: ").strip()
        title = title or "SmartScope Globus Preprocessing"
        result = fc.create_flow(title=title, definition=definition)
        flow_id = result["id"]
        print(f"\nFlow deployed: {flow_id}")

    # Re-auth with flow-specific scope
    _reauth_for_flow(flow_id, tokens)

    return flow_id

def _reauth_for_flow(flow_id, tokens):
    """Add flow-specific run scope to tokens."""
    from globus_sdk import NativeAppAuthClient, SpecificFlowClient

    flow_scope = SpecificFlowClient(flow_id).scopes.user
    if any(flow_id in str(v) for v in tokens.values()):
        return  # Already have flow-specific tokens

    print(f"\nFlow-specific auth needed for {flow_id}")
    auth_client = NativeAppAuthClient(CLIENT_ID)
    auth_client.oauth2_start_flow(
        requested_scopes=SCOPES + [flow_scope],
        refresh_tokens=True,
    )
    url = auth_client.oauth2_get_authorize_url()
    print(f"\nVisit this URL:\n\n  {url}\n")
    code = input("Paste the authorization code: ").strip()

    token_response = auth_client.oauth2_exchange_code_for_tokens(code)
    for rs, data in token_response.by_resource_server.items():
        tokens[rs] = dict(data)
    _save_tokens(tokens)
    print("Flow-specific tokens saved.")

# ---------- Register Compute Function ----------

def do_register_function(tokens=None):
    from globus_sdk import NativeAppAuthClient, ComputeClientV2, RefreshTokenAuthorizer
    from globus_compute_sdk.sdk.client import FunctionRegistrationData

    print("\n=== Register Compute Function ===\n")

    if tokens is None:
        tokens = _load_tokens()

    if "funcx_service" not in tokens:
        print("Error: No compute token. Run 'setup_globus.py auth' first.")
        return None

    auth_client = NativeAppAuthClient(CLIENT_ID)
    t = tokens["funcx_service"]
    authorizer = RefreshTokenAuthorizer(
        t["refresh_token"], auth_client,
        access_token=t["access_token"],
        expires_at=t["expires_at_seconds"],
    )
    cc = ComputeClientV2(authorizer=authorizer)

    # Import the canonical compute function
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from register_compute_function import run_preprocessing

    reg_data = FunctionRegistrationData(function=run_preprocessing)
    result = cc.post("/v3/functions", data=reg_data.to_dict())
    func_id = result.data["function_uuid"]

    print(f"Function registered: {func_id}")
    print(f"Function name: run_preprocessing")

    return func_id

# ---------- Status ----------

def do_status():
    print("\n=== SmartScope Globus Status ===\n")

    tokens = _load_tokens()
    if not tokens:
        print(f"No tokens found at {TOKEN_FILE}")
        return

    print(f"Token file: {TOKEN_FILE}")
    print(f"Token scopes:")
    for rs in tokens:
        print(f"  - {rs}")

    print(f"\nFlow definition: {FLOW_DEFINITION_FILE}")
    print(f"  Exists: {FLOW_DEFINITION_FILE.exists()}")

# ---------- Interactive Setup ----------

def do_full_setup():
    print("=" * 50)
    print("  SmartScope Globus Pipeline Setup")
    print("=" * 50)

    # Step 1: Auth
    tokens = _load_tokens()
    if tokens and "flows.globus.org" in tokens and "funcx_service" in tokens:
        reauth = input("\nExisting tokens found. Re-authenticate? [y/N]: ").strip().lower()
        if reauth == "y":
            tokens = do_auth()
    else:
        tokens = do_auth()

    # Step 2: Deploy flow
    flow_id = do_deploy_flow(tokens)

    # Step 3: Register function
    func_id = do_register_function(tokens)

    # Summary
    print("\n" + "=" * 50)
    print("  Setup Complete")
    print("=" * 50)
    print(f"\n  Flow ID:     {flow_id}")
    print(f"  Function ID: {func_id}")
    print(f"  Token file:  {TOKEN_FILE}")
    print(f"\n  Enter these in the SmartScope preprocessing form,")
    print(f"  or they will appear in the dropdowns automatically.")
    print()


# ---------- CLI ----------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"

    commands = {
        "setup": do_full_setup,
        "auth": do_auth,
        "deploy-flow": do_deploy_flow,
        "register-func": do_register_function,
        "status": do_status,
    }

    if cmd in ("-h", "--help") or cmd not in commands:
        print(__doc__)
        sys.exit(0 if cmd in ("-h", "--help") else 1)

    commands[cmd]()
