#!/usr/bin/env python3
"""Register the Doppio Globus Compute function using existing tokens.

Usage:
    docker exec smartscope-smartscope-1 python /opt/smartscope/scripts/register_doppio_function.py
"""

import json
from pathlib import Path
from globus_sdk import NativeAppAuthClient, ComputeClientV2, RefreshTokenAuthorizer
from globus_compute_sdk.sdk.client import FunctionRegistrationData


def run_doppio_live(manifest_path: str, project_dir: str,
                    source_collection: str = "", destination_collection: str = "",
                    source_base_path: str = "", destination_base_path: str = "",
                    destination_filesystem_root: str = "") -> dict:
    """Run preprocessing for a batch of movies via SLURM.

    Delegates to the doppio-live-smartscope-batch CLI tool (installed
    on the HPC as part of the doppio-live package). This function is
    serialized by Globus Compute and must be self-contained.
    """
    import json as _json
    import subprocess

    cmd = [
        "doppio-live-smartscope-batch",
        "--manifest", manifest_path,
        "--project-dir", project_dir,
        "--source-collection", source_collection,
        "--destination-collection", destination_collection,
        "--source-base-path", source_base_path,
        "--destination-base-path", destination_base_path,
        "--destination-filesystem-root", destination_filesystem_root,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(
            f"doppio-live-smartscope-batch failed (exit {result.returncode}): "
            f"{result.stderr}"
        )

    return _json.loads(result.stdout)


if __name__ == '__main__':
    TOKEN_FILE = '/opt/config/smartscope_tokens.json'
    CLIENT_ID = '7df9d534-fb19-4d79-8e83-642f1cdcf081'

    tokens = json.loads(Path(TOKEN_FILE).read_text())
    t = tokens['funcx_service']
    auth_client = NativeAppAuthClient(CLIENT_ID)
    authorizer = RefreshTokenAuthorizer(
        t['refresh_token'], auth_client,
        access_token=t['access_token'],
        expires_at=t['expires_at_seconds'],
    )
    cc = ComputeClientV2(authorizer=authorizer)

    reg_data = FunctionRegistrationData(function=run_doppio_live)
    result = cc.post('/v3/functions', data=reg_data.to_dict())
    func_id = result.data['function_uuid']
    print(f'Function ID: {func_id}')
    print('Update the SmartScope form "Compute Function ID" field with this value.')
