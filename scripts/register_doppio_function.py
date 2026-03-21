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
    """Ensure Doppio Live orchestrator is running, wait for batch results.

    Called by Globus Compute on the HPC endpoint. Initializes a pipeliner
    project if needed, starts the live preprocessing job through pipeliner,
    then polls for the .done.json for this batch. Returns a dict with
    results metadata and transfer_items for the return transfer.
    """
    import json as _json
    import os
    import time
    from pathlib import Path as _Path

    manifest = _json.loads(_Path(manifest_path).read_text())
    batch_id = manifest.get('batch_id', 'unknown')
    config = manifest.get('config', {})

    # --- Find the live job directory (highest job number) ---
    def find_job_dir():
        live_dir = _Path(project_dir) / 'LivePreprocess'
        if not live_dir.exists():
            return live_dir / 'job001'
        jobs = sorted(live_dir.glob('job*'))
        return jobs[-1] if jobs else live_dir / 'job001'

    # --- Start orchestrator via pipeliner if not running ---
    job_dir = find_job_dir()
    pid_file = job_dir / 'orchestrator.pid'
    orchestrator_running = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            orchestrator_running = True
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    if not orchestrator_running:
        # Change to project directory (pipeliner expects this as cwd)
        os.chdir(project_dir)

        from pipeliner.project_graph import ProjectGraph, new_job_of_type
        from pipeliner.job_manager import run_job as pipeliner_run_job

        # Initialize or load the pipeliner project
        pipeline_star = _Path(project_dir) / 'default_pipeline.star'
        create_new = not pipeline_star.exists()
        pipeline = ProjectGraph(
            name='default',
            pipeline_dir=project_dir,
            read_only=False,
            create_new=create_new,
        )

        # Create the live preprocessing job and set options from manifest
        job = new_job_of_type('live.preprocessing')

        # Enable SmartScope mode
        if 'smartscope_mode' in job.joboptions:
            job.joboptions['smartscope_mode'].value = True

        # Map manifest config keys to job option keys
        # Keys match where job option name == config key
        direct_keys = [
            'movie_pattern', 'num_workers', 'batch_size',
            'worker_partition', 'worker_account', 'worker_time_limit',
            'worker_mem', 'pixel_size', 'voltage', 'cs',
            'amplitude_contrast', 'dose_per_frame', 'gain_reference',
            'motioncor_module', 'motioncor_patches', 'motioncor_binning',
            'ctf_module', 'ctf_box_size', 'defocus_min', 'defocus_max',
            'picking_module', 'picking_model', 'picking_threshold',
            'box_size', 'extract_box_size', 'extract_downscale',
            'thumbnail_size',
        ]
        for key in direct_keys:
            if key in job.joboptions and key in config:
                job.joboptions[key].value = config[key]

        # Keys that need name mapping
        if 'do_picking' in job.joboptions and 'do_picking' in config:
            job.joboptions['do_picking'].value = config['do_picking']
        if 'do_extract' in job.joboptions and 'do_extraction' in config:
            job.joboptions['do_extract'].value = config['do_extraction']

        # Run the job through pipeliner (launches orchestrator in background)
        pipeliner_run_job(pipeline, job, ignore_invalid_joboptions=True)
        pipeline.close()

        # Update job_dir since pipeliner may have created a new one
        job_dir = find_job_dir()

    # --- Wait for .done.json ---
    done_file = job_dir / 'Manifests' / f'{batch_id}.done.json'

    timeout = 3600  # 1 hour max
    poll_interval = 5
    elapsed = 0
    while elapsed < timeout:
        if done_file.exists():
            break
        time.sleep(poll_interval)
        elapsed += poll_interval

    if not done_file.exists():
        raise TimeoutError(f'Timed out waiting for {done_file} after {timeout}s')

    # --- Read results and build return transfer items ---
    done_data = _json.loads(done_file.read_text())

    # Build Globus path from filesystem path
    fs_root = destination_filesystem_root.rstrip('/')
    hpc_globus_base = project_dir.replace(fs_root, '').strip('/')
    hpc_globus_base = f'/{hpc_globus_base}'

    transfer_items = []

    # Transfer the .done.json itself
    done_rel = str(done_file).replace(project_dir, '').strip('/')
    transfer_items.append({
        'source_path': f'{hpc_globus_base}/{done_rel}',
        'destination_path': f'{source_base_path}/{done_rel}',
    })

    # Transfer thumbnails
    for mic in done_data.get('results', []):
        for key in ('thumbnail', 'ctf_thumbnail'):
            thumb = mic.get(key, '')
            if thumb:
                transfer_items.append({
                    'source_path': f'{hpc_globus_base}/{thumb.lstrip("/")}',
                    'destination_path': f'{source_base_path}/{thumb.lstrip("/")}',
                })

    return {
        'status': done_data.get('status', 'unknown'),
        'batch_id': batch_id,
        'results': done_data.get('results', []),
        'transfer_items': transfer_items,
    }


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

    # Use SDK's FunctionRegistrationData for proper serialization
    reg_data = FunctionRegistrationData(function=run_doppio_live)
    result = cc.post('/v3/functions', data=reg_data.to_dict())
    func_id = result.data['function_uuid']
    print(f'Function ID: {func_id}')
    print('Update the SmartScope form "Compute Function ID" field with this value.')
