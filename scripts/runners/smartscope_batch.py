"""SmartScope batch runner for Globus Compute.

Called by the tiny registered Globus Compute function.
Reads a manifest, builds and submits a SLURM job, waits for
completion, reads results, and prints JSON to stdout.

Usage:
    doppio-live-smartscope-batch \\
        --manifest /path/to/manifest.json \\
        --project-dir /path/to/project \\
        --source-collection UUID \\
        --destination-collection UUID \\
        --source-base-path /Superluminal/SmartScope \\
        --destination-base-path /CryoEM/Projects \\
        --destination-filesystem-root /mnt/blackmore/ext-superluminal/
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="SmartScope batch runner")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--source-collection", default="")
    parser.add_argument("--destination-collection", default="")
    parser.add_argument("--source-base-path", default="")
    parser.add_argument("--destination-base-path", default="")
    parser.add_argument("--destination-filesystem-root", default="")
    args = parser.parse_args()

    result = run_batch(
        manifest_path=args.manifest,
        project_dir=args.project_dir,
        source_collection=args.source_collection,
        destination_collection=args.destination_collection,
        source_base_path=args.source_base_path,
        destination_base_path=args.destination_base_path,
        destination_filesystem_root=args.destination_filesystem_root,
    )
    print(json.dumps(result))


def run_batch(manifest_path, project_dir, source_collection="",
              destination_collection="", source_base_path="",
              destination_base_path="", destination_filesystem_root=""):
    """Run a SmartScope preprocessing batch via SLURM."""

    manifest = json.loads(Path(manifest_path).read_text())
    batch_id = manifest.get("batch_id", "unknown")
    config = manifest.get("config", {})

    movies = [m for m in manifest.get("movies", [])
              if m.lower().endswith((".tif", ".tiff", ".mrc", ".eer"))]

    if not movies:
        return {"status": "completed", "batch_id": batch_id,
                "transfer_items": []}

    # ---- Setup ----
    job_dir = Path(config.get("output_dir",
                   str(Path(project_dir) / "LivePreprocess" / "job001")))
    for d in ["MotionCorr/Micrographs", "MotionCorr/Motion",
              "CtfFind/Micrographs", "MiFFI", "AutoPick/Micrographs",
              "Thumbnails", "CtfThumbnails", "batches", "Extract/Particles"]:
        (job_dir / d).mkdir(parents=True, exist_ok=True)

    config_path = job_dir / "live_config.json"
    config_path.write_text(json.dumps({
        "output_dir": str(job_dir), "project_dir": project_dir,
        "pixel_size": config.get("pixel_size", 1.0),
        "voltage": config.get("voltage", 300),
        "cs": config.get("cs", 2.7),
        "amplitude_contrast": config.get("amplitude_contrast", 0.07),
        "dose_per_frame": config.get("dose_per_frame", 1.0),
        "gain_reference": config.get("gain_reference", ""),
        "gain_rotation": config.get("gain_rotation", 0),
        "gain_flip": config.get("gain_flip", 0),
        "motioncor_patches": config.get("motioncor_patches", 5),
        "motioncor_binning": config.get("motioncor_binning", 1.0),
        "ctf_backend": config.get("ctf_backend", "ctffind5"),
        "ctf_box_size": config.get("ctf_box_size", 512),
        "defocus_min": config.get("defocus_min", 5000.0),
        "defocus_max": config.get("defocus_max", 50000.0),
        "do_picking": config.get("do_picking", True),
        "picking_threshold": config.get("picking_threshold", 0.3),
        "picking_model": config.get("picking_model", ""),
        "box_size": config.get("box_size", 200),
        "do_extract": config.get("do_extraction", False),
        "extract_box_size": config.get("extract_box_size", 256),
        "extract_downscale": config.get("extract_downscale", 1),
        "thumbnail_size": config.get("thumbnail_size", 512),
    }, indent=2))

    batches_dir = job_dir / "batches"
    movies_file = batches_dir / f"{batch_id}_movies.txt"
    movies_file.write_text("\n".join(movies) + "\n")

    # ---- Build SLURM script ----
    mc_results = batches_dir / f"{batch_id}_mc.json"
    ctf_results = batches_dir / f"{batch_id}_ctf.json"
    pick_results = batches_dir / f"{batch_id}_pick.json"
    extract_results = batches_dir / f"{batch_id}_extract.json"

    doppio_bin = str(Path(sys.executable).resolve().parent)
    motioncor_module = config.get("motioncor_module", "motioncor3/1.2.4")
    ctf_module = config.get("ctf_module", "ctffind/5.0.2")
    picking_module = config.get("picking_module", "cryolo/stable")

    stages = [
        ("motioncor", motioncor_module,
         f"doppio-live-stage-motioncor --config {config_path} "
         f"--movies-file {movies_file} --output-file {mc_results}"),
    ]

    if config.get("ctf_backend", "ctffind5") == "motioncor3":
        stages.append(("ctf", None, f"cp {mc_results} {ctf_results}"))
    else:
        stages.append(("ctf", ctf_module,
            f"doppio-live-stage-ctf --config {config_path} "
            f"--input-file {mc_results} --output-file {ctf_results}"))

    if config.get("do_picking", True):
        stages.append(("picking", picking_module,
            f"doppio-live-stage-picking --config {config_path} "
            f"--input-file {ctf_results} --output-file {pick_results}"))
    else:
        stages.append(("picking", None, f"cp {ctf_results} {pick_results}"))

    if config.get("do_extraction", False) and config.get("do_picking", True):
        stages.append(("extraction", None,
            f"doppio-live-stage-extraction --config {config_path} "
            f"--input-file {pick_results} --output-file {extract_results}"))
    else:
        stages.append(("extraction", None, f"cp {pick_results} {extract_results}"))

    stages.append(("finalize", None,
        f"doppio-live-stage-finalize --config {config_path} "
        f"--input-file {extract_results}"))

    script_file = batches_dir / f"{batch_id}.sh"
    combined_log = batches_dir / f"{batch_id}.log"

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=SmartScope_{batch_id}",
        f"#SBATCH --partition={config.get('worker_partition', 'gpupriority')}",
        "#SBATCH --gres=gpu:1",
        "#SBATCH --cpus-per-task=4",
        f"#SBATCH --mem={config.get('worker_mem', '32G')}",
        f"#SBATCH --time={config.get('worker_time_limit', '24:00:00')}",
        f"#SBATCH --output={combined_log}",
        f"#SBATCH --error={combined_log}",
    ]
    account = config.get("worker_account", "priority-superluminal")
    if account:
        lines.append(f"#SBATCH --account={account}")
    constraint = config.get("worker_constraint", "a40|a100")
    if constraint:
        lines.append(f'#SBATCH --constraint="{constraint}"')
    lines.append(f"\ncd {project_dir}")

    for stage_name, tool_module, command in stages:
        stage_log = batches_dir / f"{batch_id}_{stage_name}.log"
        lines.append(f"\n# === Stage: {stage_name} ===")
        if tool_module:
            lines.append("source /etc/profile.d/modules.sh")
            lines.append("module purge")
            lines.append(f"module load {tool_module}")
        lines.append(f'export PATH="{doppio_bin}:$PATH"')
        lines.append(f"{command} >> {stage_log} 2>&1 || exit 1")

    script_file.write_text("\n".join(lines) + "\n")
    script_file.chmod(0o755)

    # ---- Submit and wait ----
    result = subprocess.run(
        ["sbatch", "--wait", "--parsable", str(script_file)],
        capture_output=True, text=True, cwd=project_dir, timeout=7200,
    )
    slurm_job_id = result.stdout.strip().split(";")[0]
    if result.returncode != 0:
        raise RuntimeError(f"SLURM job {slurm_job_id} failed (exit {result.returncode})")

    # ---- Read results from finalize output ----
    # stage_finalize writes updated results back to the extract_results JSON
    results = []
    if extract_results.exists():
        results = json.loads(extract_results.read_text())

    # ---- Build transfer items ----
    fs_root = destination_filesystem_root.rstrip("/")
    hpc_globus_base = "/" + project_dir.replace(fs_root, "").strip("/")

    transfer_items = []
    for mic in results:
        for key in ("thumbnail", "ctf_thumbnail"):
            path = mic.get(key, "")
            if path:
                transfer_items.append({
                    "source_path": f"{hpc_globus_base}/{path}",
                    "destination_path": f"{source_base_path}/{path}",
                })

    # Write .done.json with full metadata
    done_file = job_dir / f"Manifests/{batch_id}.done.json"
    done_file.parent.mkdir(parents=True, exist_ok=True)
    done_file.write_text(json.dumps({
        "status": "completed", "batch_id": batch_id, "results": results,
    }, indent=2))

    done_rel = str(done_file).replace(project_dir, "").strip("/")
    transfer_items.append({
        "source_path": f"{hpc_globus_base}/{done_rel}",
        "destination_path": f"{source_base_path}/{done_rel}",
    })

    if not transfer_items:
        log_rel = str(combined_log).replace(project_dir, "").strip("/")
        transfer_items.append({
            "source_path": f"{hpc_globus_base}/{log_rel}",
            "destination_path": f"{source_base_path}/{log_rel}",
        })

    return {"status": "completed", "batch_id": batch_id,
            "transfer_items": transfer_items}


if __name__ == "__main__":
    main()
