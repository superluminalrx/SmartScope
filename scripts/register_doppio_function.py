#!/usr/bin/env python3
"""Register the Doppio Globus Compute function using existing tokens.

Usage:
    docker exec smartscope-smartscope-1 python /opt/smartscope/scripts/register_doppio_function.py
"""

import json
import base64
import dill
from pathlib import Path
from globus_sdk import NativeAppAuthClient, ComputeClientV2, RefreshTokenAuthorizer


def run_doppio_live(manifest_path: str, project_dir: str) -> str:
    """Process a batch of movies via Doppio Live on the HPC.

    Called by Globus Compute on the HPC endpoint. Loads the Doppio
    environment via module system and runs doppio-live-worker.
    """
    import subprocess
    import json as _json
    from pathlib import Path as _Path

    manifest = _json.loads(_Path(manifest_path).read_text())
    batch_id = manifest.get('batch_id', 'unknown')

    result = subprocess.run(
        ['bash', '-c',
         'source /etc/profile && '
         'export MODULEPATH=$MODULEPATH:/home/group/superluminal/software/modulefiles && '
         'module load ccp/doppio && '
         f'doppio-live-worker '
         f'--project-dir "{project_dir}" '
         f'--manifest "{manifest_path}"'],
        capture_output=True, text=True, timeout=7200
    )

    if result.returncode != 0:
        raise RuntimeError(
            f'Doppio failed for {batch_id}:\n'
            f'stdout: {result.stdout}\n'
            f'stderr: {result.stderr}'
        )

    return result.stdout.strip()


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

    # Serialize function with dill
    fn_code = base64.b64encode(dill.dumps(run_doppio_live)).decode()

    result = cc.register_function({
        'function_name': 'run_doppio_live',
        'function_code': fn_code,
    })
    func_id = result.data['function_uuid']
    print(f'Function ID: {func_id}')
    print('Update the SmartScope form "Compute Function ID" field with this value.')
