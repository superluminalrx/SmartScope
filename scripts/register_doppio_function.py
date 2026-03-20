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

    Called by Globus Compute on the HPC endpoint. Starts the orchestrator
    if not running, then polls for the .done.json for this batch. Returns
    a dict with results metadata and transfer_items for the return transfer.
    """
    import subprocess
    import json as _json
    import os
    import time
    from pathlib import Path as _Path

    manifest = _json.loads(_Path(manifest_path).read_text())
    batch_id = manifest.get('batch_id', 'unknown')
    config = manifest.get('config', {})

    job_dir = _Path(project_dir) / 'LivePreprocess' / 'job001'
    config_path = job_dir / 'live_config.json'
    pid_file = job_dir / 'orchestrator.pid'
    done_file = job_dir / 'Manifests' / f'{batch_id}.done.json'

    # --- Start orchestrator if not running ---
    orchestrator_running = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            orchestrator_running = True
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    if not orchestrator_running:
        # Create job directory structure
        for subdir in ['MotionCorr/Micrographs', 'CtfFind/Micrographs',
                       'MiFFI', 'AutoPick/Micrographs', 'Thumbnails',
                       'CtfThumbnails', 'work_queue', 'workers', 'Manifests',
                       'Extract/Particles']:
            (job_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Build orchestrator config
        # In SmartScope mode, don't set watch_directories — movies come
        # from manifests only. The file watcher would race against the
        # manifest scanner and process movies before the manifest is read.
        live_config = {
            'output_dir': str(job_dir),
            'project_dir': project_dir,
            'watch_directories': [],
            'scan_subdirs': False,
            'movie_pattern': '*.tif',
            'smartscope_mode': True,
            'num_workers': config.get('num_workers', 2),
            'batch_size': config.get('batch_size', 10),
            'gpus_per_worker': config.get('gpus_per_worker', 1),
            'worker_partition': config.get('worker_partition', 'gpupriority'),
            'worker_account': config.get('worker_account', 'priority-superluminal'),
            'worker_time_limit': config.get('worker_time_limit', '24:00:00'),
            'worker_mem': config.get('worker_mem', '32G'),
            'pixel_size': config.get('pixel_size', 1.0),
            'voltage': config.get('voltage', 300),
            'cs': config.get('cs', 2.7),
            'amplitude_contrast': config.get('amplitude_contrast', 0.07),
            'dose_per_frame': config.get('dose_per_frame', 1.0),
            'gain_reference': config.get('gain_reference', ''),
            'gain_rotation': config.get('gain_rotation', 0),
            'gain_flip': config.get('gain_flip', 0),
            'motioncor_backend': config.get('motioncor_backend', 'motioncor3'),
            'motioncor_module': config.get('motioncor_module', 'motioncor3/1.2.4'),
            'motioncor_patches': config.get('motioncor_patches', 5),
            'motioncor_binning': config.get('motioncor_binning', 1.0),
            'ctf_backend': config.get('ctf_backend', 'ctffind5'),
            'ctf_module': config.get('ctf_module', 'ctffind/5.0.2'),
            'ctf_box_size': config.get('ctf_box_size', 512),
            'defocus_min': config.get('defocus_min', 5000.0),
            'defocus_max': config.get('defocus_max', 50000.0),
            'do_picking': config.get('do_picking', True),
            'picking_backend': config.get('picking_backend', 'cryolo'),
            'picking_module': config.get('picking_module', 'cryolo/stable'),
            'picking_model': config.get('picking_model', ''),
            'picking_threshold': config.get('picking_threshold', 0.3),
            'box_size': config.get('box_size', 200),
            'do_extract': config.get('do_extract', False),
            'extract_box_size': config.get('extract_box_size', 256),
            'extract_downscale': config.get('extract_downscale', 1),
            'thumbnail_size': config.get('thumbnail_size', 512),
        }
        config_path.write_text(_json.dumps(live_config, indent=2))

        subprocess.Popen(
            ['bash', '-c',
             'source /etc/profile && '
             'export MODULEPATH=$MODULEPATH:/home/group/superluminal/software/modulefiles && '
             'module load ccp/doppio && '
             f'doppio-live-orchestrator --config "{config_path}" '
             f'> "{job_dir}/orchestrator.log" 2>&1 &\n'
             f'echo $! > "{pid_file}"'],
            start_new_session=True,
        )

    # --- Wait for .done.json ---
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

    # Build list of files to transfer back (HPC → DTN)
    # Paths are relative to project_dir in the done_data
    fs_root = destination_filesystem_root.rstrip('/')
    dest_base = destination_base_path.rstrip('/')
    project_rel = project_dir.replace(fs_root, '').strip('/')
    hpc_globus_base = f'{dest_base}/{project_rel}'.rstrip('/')

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
