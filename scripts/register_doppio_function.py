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
    """Run a Doppio preprocessing job for a batch of movies.

    Called by Globus Compute on the HPC endpoint. Each BIS group gets its
    own pipeliner job. Initializes the pipeliner project if needed, writes
    a movies file, creates and runs a live.preprocessing job in SmartScope
    mode (no orchestrator), and returns results + transfer items.
    """
    import json as _json
    import os
    import subprocess
    import textwrap
    from pathlib import Path as _Path

    manifest = _json.loads(_Path(manifest_path).read_text())
    batch_id = manifest.get('batch_id', 'unknown')
    config = manifest.get('config', {})

    # Filter to only movie files (not .mdoc etc)
    movies = [m for m in manifest.get('movies', [])
              if m.lower().endswith(('.tif', '.tiff', '.mrc', '.eer'))]

    if not movies:
        return {'status': 'completed', 'batch_id': batch_id,
                'results': [], 'transfer_items': []}

    # Write movies file to project dir
    movies_file = _Path(project_dir) / f'.smartscope_movies_{batch_id}.txt'
    movies_file.write_text('\n'.join(movies) + '\n')

    # Write launcher script that runs inside Doppio environment
    launcher_script = _Path(project_dir) / f'.smartscope_launch_{batch_id}.py'
    launcher_script.write_text(textwrap.dedent(f'''\
        import json, os
        from pathlib import Path
        os.chdir({project_dir!r})

        from pipeliner.project_graph import ProjectGraph, new_job_of_type
        from pipeliner.job_manager import run_job

        config = json.loads({_json.dumps(config)!r})

        pipeline_star = Path("default_pipeline.star")
        create_new = not pipeline_star.exists()
        pipeline = ProjectGraph(
            name="default",
            pipeline_dir=".",
            read_only=False,
            create_new=create_new,
        )

        job = new_job_of_type("live.preprocessing")

        # SmartScope mode: process movies file directly, no orchestrator
        if "smartscope_mode" in job.joboptions:
            job.joboptions["smartscope_mode"].value = True
        if "movies_file" in job.joboptions:
            job.joboptions["movies_file"].value = {str(movies_file)!r}

        # Set job options from manifest config
        direct_keys = [
            "pixel_size", "voltage", "cs", "amplitude_contrast",
            "dose_per_frame", "gain_reference",
            "motioncor_patches", "motioncor_binning",
            "ctf_box_size", "defocus_min", "defocus_max",
            "picking_model", "picking_threshold", "box_size",
            "extract_box_size", "extract_downscale", "thumbnail_size",
        ]
        for key in direct_keys:
            if key in job.joboptions and key in config:
                job.joboptions[key].value = config[key]

        if "do_picking" in job.joboptions and "do_picking" in config:
            job.joboptions["do_picking"].value = config["do_picking"]
        if "do_extract" in job.joboptions and "do_extraction" in config:
            job.joboptions["do_extract"].value = config["do_extraction"]

        # Run to completion (foreground)
        run_job(pipeline, job, ignore_invalid_joboptions=True,
                run_in_foreground=True)
        pipeline.close()

        # Find the job directory that was created
        job_dirs = sorted(Path("LivePreprocess").glob("job*"))
        print("JOB_DIR=" + str(job_dirs[-1]) if job_dirs else "JOB_DIR=NONE")
    '''))

    shell_script = _Path(project_dir) / f'.smartscope_launch_{batch_id}.sh'
    shell_script.write_text(
        '#!/bin/bash\n'
        'source /etc/profile\n'
        'export MODULEPATH=$MODULEPATH:/home/group/superluminal/software/modulefiles\n'
        'module load ccp/doppio\n'
        f'exec python3 "{launcher_script}"\n'
    )
    os.chmod(str(shell_script), 0o755)

    result = subprocess.run(
        [str(shell_script)],
        capture_output=True, text=True, timeout=3600,
    )

    # Clean up temp scripts
    launcher_script.unlink(missing_ok=True)
    shell_script.unlink(missing_ok=True)
    movies_file.unlink(missing_ok=True)

    # Parse job directory from output
    job_dir = None
    for line in result.stdout.splitlines():
        if line.startswith('JOB_DIR='):
            job_dir = _Path(project_dir) / line.split('=', 1)[1]
            break

    if result.returncode != 0 or job_dir is None or str(job_dir) == 'NONE':
        raise RuntimeError(
            f'Pipeliner job failed:\nstdout: {result.stdout}\nstderr: {result.stderr}')

    # --- Read results from STAR file and build transfer items ---
    # The worker writes results to micrographs_ctf.star. Build a
    # results dict compatible with SmartScope's _update_db_from_done.
    results = []
    thumbnails_dir = job_dir / 'Thumbnails'
    ctf_thumbnails_dir = job_dir / 'CtfThumbnails'

    try:
        import gemmi
        star_path = job_dir / 'micrographs_ctf.star'
        if star_path.exists():
            doc = gemmi.cif.read(str(star_path))
            block = doc.find_block('micrographs')
            if block:
                tags = block.find([
                    '_rlnMicrographMovieName', '_rlnMicrographName',
                    '_rlnDefocusU', '_rlnDefocusV', '_rlnDefocusAngle',
                    '_rlnCtfMaxResolution', '_rlnCtfFigureOfMerit',
                    '_rlnAccumMotionTotal', '_rlnCtfIceThickness',
                ])
                for row in tags:
                    mic_stem = _Path(row[1]).stem
                    thumb = thumbnails_dir / f'{mic_stem}.png'
                    ctf_thumb = ctf_thumbnails_dir / f'{mic_stem}_Ctf.png'
                    results.append({
                        'movie': row[0],
                        'micrograph': row[1],
                        'defocus_u': float(row[2]) if row[2] != '.' else 0.0,
                        'defocus_v': float(row[3]) if row[3] != '.' else 0.0,
                        'defocus_angle': float(row[4]) if row[4] != '.' else 0.0,
                        'ctf_max_resolution': float(row[5]) if row[5] != '.' else 999.0,
                        'ctf_fom': float(row[6]) if row[6] != '.' else 0.0,
                        'total_motion': float(row[7]) if row[7] != '.' else 0.0,
                        'ice_thickness': float(row[8]) if row[8] != '.' else 0.0,
                        'thumbnail': str(thumb.relative_to(_Path(project_dir)))
                                     if thumb.exists() else '',
                        'ctf_thumbnail': str(ctf_thumb.relative_to(_Path(project_dir)))
                                         if ctf_thumb.exists() else '',
                    })
    except Exception:
        pass  # gemmi not available in Globus Compute env — results will be empty

    # Build Globus transfer items
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

    return {
        'status': 'completed',
        'batch_id': batch_id,
        'results': results,
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
