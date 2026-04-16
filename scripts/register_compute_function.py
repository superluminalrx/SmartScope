#!/usr/bin/env python3
"""Register the SmartScope Globus Compute function using existing tokens.

Usage:
    docker exec smartscope-smartscope-1 python /opt/smartscope/scripts/register_compute_function.py
"""

import json
from pathlib import Path
from globus_sdk import NativeAppAuthClient, ComputeClientV2, RefreshTokenAuthorizer
from globus_compute_sdk.sdk.client import FunctionRegistrationData


def run_preprocessing(manifest_path: str, project_dir: str,
                    source_collection: str = "", destination_collection: str = "",
                    source_base_path: str = "", destination_base_path: str = "",
                    destination_filesystem_root: str = "") -> dict:
    """Run preprocessing for a batch of movies via SLURM.

    Called by Globus Compute on the HPC login node. Delegates to the
    doppio-live-smartscope-batch CLI which builds and submits a SLURM
    job, waits for completion, and returns transfer items.

    The CLI uses the shared batch_submit module so the SLURM script
    is identical to what the Doppio orchestrator produces.
    """
    import json as _json
    import subprocess

    # Read the manifest to get the doppio module name (configurable per site)
    manifest = _json.loads(open(manifest_path).read())
    doppio_module = manifest.get("config", {}).get(
        "doppio_module", "ccp/doppio/stable")

    # Build a shell command that loads the doppio module first
    batch_cmd = (
        "doppio-live-smartscope-batch"
        f" --manifest {manifest_path}"
        f" --project-dir {project_dir}"
        f" --source-collection {source_collection}"
        f" --destination-collection {destination_collection}"
        f" --source-base-path {source_base_path}"
        f" --destination-base-path {destination_base_path}"
        f" --destination-filesystem-root {destination_filesystem_root}"
    )
    shell_cmd = (
        f"source /etc/profile.d/modules.sh && "
        f"module load {doppio_module} && "
        f"{batch_cmd}"
    )
    result = subprocess.run(
        ["bash", "-c", shell_cmd],
        capture_output=True, text=True, timeout=7200,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"doppio-live-smartscope-batch failed (exit {result.returncode}):\n"
            f"{result.stderr[-2000:]}"
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

    reg_data = FunctionRegistrationData(function=run_preprocessing)
    result = cc.post('/v3/functions', data=reg_data.to_dict())
    func_id = result.data['function_uuid']
    print(f'Function ID: {func_id}')
    print('Update the SmartScope form "Compute Function ID" field with this value.')
