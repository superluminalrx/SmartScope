#!/usr/bin/env python3
"""Re-auth for a Globus Flow. Prints URL, waits for code on stdin."""
import json
import sys
from pathlib import Path
from globus_sdk import NativeAppAuthClient, SpecificFlowClient, FlowsClient

CLIENT_ID = '7df9d534-fb19-4d79-8e83-642f1cdcf081'
FLOW_ID = 'd968b358-6aa0-42e8-914b-f041508b5a45'
TOKEN_FILE = '/opt/config/smartscope_tokens.json'

all_scopes = [
    'urn:globus:auth:scope:transfer.api.globus.org:all',
    SpecificFlowClient(FLOW_ID).scopes.user,
    'https://auth.globus.org/scopes/eec9b274-0c81-4334-bdc2-54e90e689b9a/manage_flows',
    'https://auth.globus.org/scopes/eec9b274-0c81-4334-bdc2-54e90e689b9a/run_manage',
    'https://auth.globus.org/scopes/eec9b274-0c81-4334-bdc2-54e90e689b9a/view_flows',
]

auth = NativeAppAuthClient(CLIENT_ID)
auth.oauth2_start_flow(requested_scopes=all_scopes, refresh_tokens=True)
print(auth.oauth2_get_authorize_url())
print()
code = input('Paste code: ').strip()

token_response = auth.oauth2_exchange_code_for_tokens(code)

existing = json.loads(Path(TOKEN_FILE).read_text()) if Path(TOKEN_FILE).exists() else {}
for rs, data in token_response.by_resource_server.items():
    existing[rs] = dict(data)
    print(f'Token for: {rs}')

Path(TOKEN_FILE).write_text(json.dumps(existing, indent=2))
Path(TOKEN_FILE).chmod(0o600)
print('Tokens saved.')
