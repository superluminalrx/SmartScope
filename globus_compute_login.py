#!/usr/bin/env python3
"""Login to Globus Compute and register the Doppio processing function.

Run inside the SmartScope container:
    docker exec -it smartscope-smartscope-1 python /opt/smartscope/globus_compute_login.py
"""
from globus_compute_sdk import Client


def run_doppio_live(manifest_path: str, project_dir: str) -> str:
    """Process a batch of movies via Doppio Live on the HPC.

    Args:
        manifest_path: Absolute path to the manifest JSON on the HPC.
        project_dir: Absolute path to the Doppio project directory.

    Returns:
        Path to the .done.json results file.
    """
    import subprocess
    import json
    from pathlib import Path

    manifest = json.loads(Path(manifest_path).read_text())
    batch_id = manifest.get("batch_id", "unknown")

    result = subprocess.run(
        ["doppio-live-worker",
         "--project-dir", project_dir,
         "--manifest", manifest_path],
        capture_output=True, text=True, timeout=7200,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Doppio processing failed for {batch_id}: {result.stderr}")

    done_file = Path(project_dir) / "LivePreprocess" / "job001" / f"{batch_id}.done.json"
    if not done_file.exists():
        raise FileNotFoundError(f"Expected results at {done_file} but not found")

    return str(done_file)


if __name__ == "__main__":
    # This will prompt for interactive login on first run
    gc = Client()
    func_id = gc.register_function(run_doppio_live)
    print(f"\nFunction registered successfully!")
    print(f"Function ID: {func_id}")
    print(f"\nAdd this to the Doppio pipeline form as the default compute_function_id.")
