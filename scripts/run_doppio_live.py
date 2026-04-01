#!/usr/bin/env python3
"""Standalone runner for SmartScope Doppio live preprocessing.

Called by the Globus Compute wrapper function. Contains all the logic
that was previously serialized into the registered function.

Usage:
    python3 run_doppio_live.py <manifest_path> <project_dir> \
        <source_collection> <destination_collection> \
        <source_base_path> <destination_base_path> \
        <destination_filesystem_root>

Prints JSON result to stdout.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    (manifest_path, project_dir, source_collection, destination_collection,
     source_base_path, destination_base_path, destination_filesystem_root) = sys.argv[1:]

    manifest = json.loads(Path(manifest_path).read_text())
    batch_id = manifest.get('batch_id', 'unknown')
    config = manifest.get('config', {})

    movies = [m for m in manifest.get('movies', [])
              if m.lower().endswith(('.tif', '.tiff', '.mrc', '.eer'))]

    if not movies:
        print(json.dumps({'status': 'completed', 'batch_id': batch_id,
                          'results': [], 'transfer_items': []}))
        return

    job_dir = Path(config.get('output_dir',
                   str(Path(project_dir) / 'LivePreprocess' / 'job001')))
    for subdir in ['MotionCorr/Micrographs', 'MotionCorr/Motion',
                   'CtfFind/Micrographs', 'MiFFI', 'AutoPick/Micrographs',
                   'Thumbnails', 'CtfThumbnails', 'batches', 'Extract/Particles']:
        (job_dir / subdir).mkdir(parents=True, exist_ok=True)

    config_data = {
        'output_dir': str(job_dir), 'project_dir': project_dir,
        'pixel_size': config.get('pixel_size', 1.0),
        'voltage': config.get('voltage', 300),
        'cs': config.get('cs', 2.7),
        'amplitude_contrast': config.get('amplitude_contrast', 0.07),
        'dose_per_frame': config.get('dose_per_frame', 1.0),
        'gain_reference': config.get('gain_reference', ''),
        'gain_rotation': config.get('gain_rotation', 0),
        'gain_flip': config.get('gain_flip', 0),
        'motioncor_patches': config.get('motioncor_patches', 5),
        'motioncor_binning': config.get('motioncor_binning', 1.0),
        'ctf_backend': config.get('ctf_backend', 'ctffind5'),
        'ctf_box_size': config.get('ctf_box_size', 512),
        'defocus_min': config.get('defocus_min', 5000.0),
        'defocus_max': config.get('defocus_max', 50000.0),
        'do_picking': config.get('do_picking', True),
        'picking_threshold': config.get('picking_threshold', 0.3),
        'picking_model': config.get('picking_model', ''),
        'box_size': config.get('box_size', 200),
        'do_extract': config.get('do_extraction', False),
        'extract_box_size': config.get('extract_box_size', 256),
        'extract_downscale': config.get('extract_downscale', 1),
        'thumbnail_size': config.get('thumbnail_size', 512),
    }
    config_path = job_dir / 'live_config.json'
    config_path.write_text(json.dumps(config_data, indent=2))

    movies_file = job_dir / 'batches' / f'{batch_id}_movies.txt'
    movies_file.write_text('\n'.join(movies) + '\n')

    batches_dir = job_dir / 'batches'
    mc_results = batches_dir / f'{batch_id}_mc.json'
    ctf_results = batches_dir / f'{batch_id}_ctf.json'
    pick_results = batches_dir / f'{batch_id}_pick.json'
    extract_results = batches_dir / f'{batch_id}_extract.json'

    doppio_bin = '/home/group/superluminal/software/moduleapps/ccp/doppio/stable/bin'
    motioncor_module = config.get('motioncor_module', 'motioncor3/1.2.4')
    ctf_module = config.get('ctf_module', 'ctffind/5.0.2')
    picking_module = config.get('picking_module', 'cryolo/stable')
    worker_partition = config.get('worker_partition', 'gpupriority')
    worker_account = config.get('worker_account', 'priority-superluminal')
    worker_time = config.get('worker_time_limit', '24:00:00')
    worker_mem = config.get('worker_mem', '32G')

    stages = [('motioncor', motioncor_module,
        f'doppio-live-stage-motioncor --config {config_path} '
        f'--movies-file {movies_file} --output-file {mc_results}')]

    ctf_backend = config.get('ctf_backend', 'ctffind5')
    if ctf_backend == 'motioncor3':
        stages.append(('ctf', None, f'cp {mc_results} {ctf_results}'))
    else:
        stages.append(('ctf', ctf_module,
            f'doppio-live-stage-ctf --config {config_path} '
            f'--input-file {mc_results} --output-file {ctf_results}'))

    if config.get('do_picking', True):
        stages.append(('picking', picking_module,
            f'doppio-live-stage-picking --config {config_path} '
            f'--input-file {ctf_results} --output-file {pick_results}'))
    else:
        stages.append(('picking', None, f'cp {ctf_results} {pick_results}'))

    if config.get('do_extraction', False) and config.get('do_picking', True):
        stages.append(('extraction', None,
            f'doppio-live-stage-extraction --config {config_path} '
            f'--input-file {pick_results} --output-file {extract_results}'))
    else:
        stages.append(('extraction', None, f'cp {pick_results} {extract_results}'))

    stages.append(('finalize', None,
        f'doppio-live-stage-finalize --config {config_path} '
        f'--input-file {extract_results}'))

    script_file = batches_dir / f'{batch_id}.sh'
    combined_log = batches_dir / f'{batch_id}.log'

    lines = [
        '#!/bin/bash',
        f'#SBATCH --job-name=SmartScope_{batch_id}',
        f'#SBATCH --partition={worker_partition}',
        '#SBATCH --gres=gpu:1',
        '#SBATCH --cpus-per-task=4',
        f'#SBATCH --mem={worker_mem}',
        f'#SBATCH --time={worker_time}',
        f'#SBATCH --output={combined_log}',
        f'#SBATCH --error={combined_log}',
    ]
    if worker_account:
        lines.append(f'#SBATCH --account={worker_account}')
    lines.append('#SBATCH --constraint="a40|a100"')
    lines.append(f'\ncd {project_dir}')

    for stage_name, tool_module, command in stages:
        stage_log = batches_dir / f'{batch_id}_{stage_name}.log'
        lines.append(f'\n# === Stage: {stage_name} ===')
        if tool_module:
            lines.append('source /etc/profile.d/modules.sh')
            lines.append('module purge')
            lines.append(f'module load {tool_module}')
        lines.append(f'export PATH="{doppio_bin}:$PATH"')
        lines.append(f'{command} >> {stage_log} 2>&1 || exit 1')

    script_file.write_text('\n'.join(lines) + '\n')
    script_file.chmod(0o755)

    result = subprocess.run(['sbatch', '--parsable', str(script_file)],
                            capture_output=True, text=True, cwd=project_dir)
    if result.returncode != 0:
        raise RuntimeError(f'sbatch failed: {result.stderr}')

    slurm_job_id = result.stdout.strip()
    state = 'UNKNOWN'
    elapsed = 0
    while elapsed < 3600:
        time.sleep(10)
        elapsed += 10
        check = subprocess.run(
            ['sacct', '-j', slurm_job_id, '--format=State', '--noheader', '-P'],
            capture_output=True, text=True)
        states = [s.strip() for s in check.stdout.strip().split('\n') if s.strip()]
        if states:
            state = states[0]
        if state in ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL'):
            break

    if state != 'COMPLETED':
        raise RuntimeError(f'SLURM job {slurm_job_id} ended with state: {state}')

    results_file = job_dir / f'.results_{batch_id}.json'
    read_script = Path(project_dir) / f'.read_results_{batch_id}.sh'
    read_script.write_text(
        '#!/bin/bash\n'
        'source /etc/profile\n'
        'export MODULEPATH=$MODULEPATH:/home/group/superluminal/software/modulefiles\n'
        'module load ccp/doppio\n'
        f'python3 -c "\n'
        f'import gemmi, json\n'
        f'from pathlib import Path\n'
        f'star = Path(\'{job_dir}/micrographs_ctf.star\')\n'
        f'results = []\n'
        f'if star.exists():\n'
        f'    doc = gemmi.cif.read(str(star))\n'
        f'    block = doc.find_block(\'micrographs\')\n'
        f'    if block:\n'
        f'        tags = block.find([\'_rlnMicrographMovieName\',\'_rlnMicrographName\',\'_rlnDefocusU\',\'_rlnDefocusV\',\'_rlnDefocusAngle\',\'_rlnCtfMaxResolution\',\'_rlnCtfFigureOfMerit\',\'_rlnAccumMotionTotal\',\'_rlnCtfIceThickness\'])\n'
        f'        for row in tags:\n'
        f'            stem = Path(row[1]).stem\n'
        f'            thumb = Path(\'{job_dir}/Thumbnails/\' + stem + \'.png\')\n'
        f'            ctf_thumb = Path(\'{job_dir}/CtfThumbnails/\' + stem + \'_Ctf.png\')\n'
        f'            results.append(dict(movie=row[0],micrograph=row[1],defocus_u=float(row[2]) if row[2]!=\'.\'else 0.0,defocus_v=float(row[3]) if row[3]!=\'.\'else 0.0,defocus_angle=float(row[4]) if row[4]!=\'.\'else 0.0,ctf_max_resolution=float(row[5]) if row[5]!=\'.\'else 999.0,ctf_fom=float(row[6]) if row[6]!=\'.\'else 0.0,total_motion=float(row[7]) if row[7]!=\'.\'else 0.0,ice_thickness=float(row[8]) if row[8]!=\'.\'else 0.0,thumbnail=str(thumb.relative_to(Path(\'{project_dir}\'))) if thumb.exists() else \'\',ctf_thumbnail=str(ctf_thumb.relative_to(Path(\'{project_dir}\'))) if ctf_thumb.exists() else \'\'))\n'
        f'Path(\'{results_file}\').write_text(json.dumps(results))\n'
        f'"\n'
    )
    os.chmod(str(read_script), 0o755)
    subprocess.run([str(read_script)], capture_output=True, timeout=60)
    read_script.unlink(missing_ok=True)

    results = []
    if results_file.exists():
        results = json.loads(results_file.read_text())
        results_file.unlink(missing_ok=True)

    fs_root = destination_filesystem_root.rstrip('/')
    hpc_globus_base = '/' + project_dir.replace(fs_root, '').strip('/')

    transfer_items = []
    for mic in results:
        for key in ('thumbnail', 'ctf_thumbnail'):
            path = mic.get(key, '')
            if path:
                transfer_items.append({
                    'source_path': f'{hpc_globus_base}/{path}',
                    'destination_path': f'{source_base_path}/{path}',
                })

    done_file = job_dir / f'Manifests/{batch_id}.done.json'
    done_file.parent.mkdir(parents=True, exist_ok=True)
    done_file.write_text(json.dumps({
        'status': 'completed', 'batch_id': batch_id, 'results': results,
    }, indent=2))

    done_rel = str(done_file).replace(project_dir, '').strip('/')
    transfer_items.append({
        'source_path': f'{hpc_globus_base}/{done_rel}',
        'destination_path': f'{source_base_path}/{done_rel}',
    })

    if not transfer_items:
        log_rel = str(combined_log).replace(project_dir, '').strip('/')
        transfer_items.append({
            'source_path': f'{hpc_globus_base}/{log_rel}',
            'destination_path': f'{source_base_path}/{log_rel}',
        })

    print(json.dumps({'status': 'completed', 'batch_id': batch_id,
                      'transfer_items': transfer_items}))


if __name__ == '__main__':
    main()
