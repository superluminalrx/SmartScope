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
    """Ensure Doppio Live orchestrator is running for this project.

    Called by Globus Compute on the HPC endpoint. Checks if the
    orchestrator is already running; if not, starts it. The orchestrator
    picks up manifests from Manifests/ and processes them via SLURM.

    The manifest file has already been transferred by the Globus Flow.
    """
    import subprocess
    import json as _json
    import os
    import signal
    from pathlib import Path as _Path

    manifest = _json.loads(_Path(manifest_path).read_text())
    batch_id = manifest.get('batch_id', 'unknown')
    config = manifest.get('config', {})

    job_dir = _Path(project_dir) / 'LivePreprocess' / 'job001'
    config_path = job_dir / 'live_config.json'
    pid_file = job_dir / 'orchestrator.pid'

    # Check if orchestrator is already running
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            return f'Orchestrator already running (PID {pid}), manifest {batch_id} will be picked up'
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    # Create job directory structure
    for subdir in ['MotionCorr/Micrographs', 'CtfFind/Micrographs',
                   'MiFFI', 'AutoPick/Micrographs', 'Thumbnails',
                   'CtfThumbnails', 'work_queue', 'workers', 'Manifests',
                   'Extract/Particles']:
        (job_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Build orchestrator config from manifest config + defaults
    live_config = {
        'output_dir': str(job_dir),
        'project_dir': project_dir,
        'watch_directories': [str(_Path(project_dir) / 'Movies')],
        'scan_subdirs': True,
        'movie_pattern': '*.tif',
        'smartscope_mode': True,
        # Worker settings
        'num_workers': config.get('num_workers', 2),
        'batch_size': config.get('batch_size', 10),
        'gpus_per_worker': config.get('gpus_per_worker', 1),
        'worker_partition': config.get('worker_partition', 'gpupriority'),
        'worker_account': config.get('worker_account', 'priority-superluminal'),
        'worker_time_limit': config.get('worker_time_limit', '24:00:00'),
        'worker_mem': config.get('worker_mem', '32G'),
        # Pipeline settings from SmartScope
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

    # Start orchestrator as background process
    result = subprocess.Popen(
        ['bash', '-c',
         'source /etc/profile && '
         'export MODULEPATH=$MODULEPATH:/home/group/superluminal/software/modulefiles && '
         'module load ccp/doppio && '
         f'doppio-live-orchestrator --config "{config_path}" '
         f'> "{job_dir}/orchestrator.log" 2>&1 &'
         f'echo $! > "{pid_file}"'],
        start_new_session=True,
    )

    return f'Orchestrator started for project {project_dir}, manifest {batch_id} will be processed'


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
