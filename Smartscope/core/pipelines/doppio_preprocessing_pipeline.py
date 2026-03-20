
import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Set

from django.db import transaction

from Smartscope.core.db_manipulations import websocket_update
from Smartscope.core.frames import get_smartscope_frames_dir
from Smartscope.core.models.grid import AutoloaderGrid
from Smartscope.core.models.high_mag import HighMagModel
from Smartscope.core.models.hole import HoleModel
from Smartscope.core.models.models_actions import update_fields

from .preprocessing_pipeline import PreprocessingPipeline
from .doppio_cmd_kwargs import DoppioCmdKwargs
from .doppio_pipeline_form import DoppioPipelineForm

logger = logging.getLogger(__name__)

TRANSFER_SCOPE = "urn:globus:auth:scope:transfer.api.globus.org:all"
FLOWS_SCOPE = "https://auth.globus.org/scopes/eec9b274-0c81-4334-bdc2-54e90e689b9e/flow_user"



# ---- Globus Auth Helpers ----

def _load_tokens(token_file: str) -> dict:
    path = Path(token_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"No cached Globus tokens at {path}. "
            f"Run: python globus_login.py get-url && python globus_login.py exchange <code>"
        )
    return json.loads(path.read_text())


def _save_tokens(token_file: str, tokens: dict):
    path = Path(token_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2))
    path.chmod(0o600)


def _build_transfer_client(cmd_data: DoppioCmdKwargs):
    from globus_sdk import NativeAppAuthClient, TransferClient, RefreshTokenAuthorizer

    tokens = _load_tokens(cmd_data.token_file)
    transfer_tokens = tokens["transfer.api.globus.org"]

    auth_client = NativeAppAuthClient(cmd_data.globus_client_id)
    authorizer = RefreshTokenAuthorizer(
        transfer_tokens["refresh_token"],
        auth_client,
        access_token=transfer_tokens["access_token"],
        expires_at=transfer_tokens["expires_at_seconds"],
        on_refresh=lambda td: _save_tokens(cmd_data.token_file, {
            **_load_tokens(cmd_data.token_file),
            "transfer.api.globus.org": td.by_resource_server["transfer.api.globus.org"],
        }),
    )
    return TransferClient(authorizer=authorizer)


def _build_flows_client(cmd_data: DoppioCmdKwargs):
    """Build a Globus Flows client (SpecificFlowClient) for running flows."""
    from globus_sdk import NativeAppAuthClient, SpecificFlowClient, RefreshTokenAuthorizer

    tokens = _load_tokens(cmd_data.token_file)
    # The flow-specific scope tokens are keyed by the flow's UUID.
    # Fall back to flows.globus.org if flow-specific tokens aren't available.
    flow_tokens = tokens.get(
        cmd_data.globus_flow_id,
        tokens.get("flows.globus.org", tokens.get("transfer.api.globus.org"))
    )

    auth_client = NativeAppAuthClient(cmd_data.globus_client_id)
    authorizer = RefreshTokenAuthorizer(
        flow_tokens["refresh_token"],
        auth_client,
        access_token=flow_tokens["access_token"],
        expires_at=flow_tokens["expires_at_seconds"],
    )
    return SpecificFlowClient(cmd_data.globus_flow_id, authorizer=authorizer)


# ---- Path Mapping ----

def _container_to_globus_path(container_path: str, source_base_path: str,
                              container_root: str = "") -> str:
    """Convert a container path to a Globus collection path.

    Args:
        container_path: Full path inside the container
        source_base_path: Globus collection prefix (e.g. "/SmartScope")
        container_root: Container-side prefix to strip (e.g. "/mnt/arctica/Superluminal/SmartScope")
                        Derived from detector.frames_directory at runtime.
    """
    rel = container_path
    if container_root and rel.startswith(container_root):
        rel = rel[len(container_root):]
    return f"{source_base_path.rstrip('/')}/{rel.lstrip('/')}"


def _globus_dest_path(container_path: str, destination_base_path: str,
                      project_path: str, grid_id: str) -> str:
    """Build destination path on HPC.

    e.g. /data/programs/Lodos/Apoferritin/Movies/grid_1/frame.tif

    Args:
        container_path: Frame path inside container (e.g. /mnt/data/.../frame.tif)
        destination_base_path: HPC root (e.g. /data/programs)
        project_path: User-defined project path from slot mapping (e.g. Lodos/Apoferritin)
        grid_id: Grid identifier for subdirectory
    """
    filename = Path(container_path).name
    return (f"{destination_base_path.rstrip('/')}/"
            f"{project_path.strip('/')}/"
            f"Movies/{grid_id}/{filename}")


class DoppioPreprocessingPipeline(PreprocessingPipeline):

    verbose_name = 'Doppio Preprocessing Pipeline (Globus)'
    name = 'doppioPipeline'
    description = 'GPU preprocessing via Globus Flows to transfer and process on HPC with Doppio.'

    cmdkwargs_handler = DoppioCmdKwargs
    pipeline_form = DoppioPipelineForm

    incomplete_processes: List = []
    to_update: List = []

    def __init__(self, grid: AutoloaderGrid, cmd_data: Dict):
        super().__init__(grid=grid)
        self.microscope = self.grid.session_id.microscope_id
        self.detector = self.grid.session_id.detector_id
        self.cmd_data = self.cmdkwargs_handler.parse_obj(cmd_data)

        # Resolve this grid's Doppio project from slot mapping
        self.grid_position = getattr(self.grid, 'position', None)
        self.project_path = ""
        if self.grid_position:
            self.project_path = self.cmd_data.get_project_for_slot(self.grid_position)

        # Frames directory for this grid (where SerialEM writes .tif/.mrc files)
        self.frames_dir = get_smartscope_frames_dir(self.grid)

        # Container-side roots to strip when building Globus paths.
        # Frames and data may be mounted differently but map to the same Globus collection.
        # frames: /mnt/arctica/Superluminal/SmartScope/... -> /SmartScope/...
        # data:   /mnt/data/...                            -> /SmartScope/...
        self.container_frames_root = str(self.detector.frames_directory).rstrip('/')
        self.container_data_root = str(self.grid.directory).rsplit(
            str(self.grid.session_id.working_directory), 1)[0].rstrip('/')

        self._stop = threading.Event()
        self._submitted_groups: Set[str] = set()  # track submitted grouping keys
        self._active_flow_runs: Dict[str, dict] = {}  # flow_run_id -> {batch, group_key}

        self.tc = None       # TransferClient (for transfer_only mode)
        self.fc = None       # SpecificFlowClient (for transfer_and_process mode)
        self.fc_general = None  # FlowsClient (for checking run status)

    # ---- Init ----

    def _init_globus_clients(self):
        self.tc = _build_transfer_client(self.cmd_data)
        logger.info("Globus Transfer client initialized")

        if self.cmd_data.mode == 'transfer_and_process' and self.cmd_data.globus_flow_id:
            try:
                self.fc = _build_flows_client(self.cmd_data)
                # Also build a general FlowsClient for checking run status
                from globus_sdk import FlowsClient
                tokens = _load_tokens(self.cmd_data.token_file)
                flows_tokens = tokens.get('flows.globus.org', {})
                if flows_tokens:
                    from globus_sdk import NativeAppAuthClient, RefreshTokenAuthorizer
                    auth_client = NativeAppAuthClient(self.cmd_data.globus_client_id)
                    authorizer = RefreshTokenAuthorizer(
                        flows_tokens['refresh_token'], auth_client,
                        access_token=flows_tokens['access_token'],
                        expires_at=flows_tokens['expires_at_seconds'],
                    )
                    self.fc_general = FlowsClient(authorizer=authorizer)
                logger.info("Globus Flows client initialized")
            except Exception as e:
                logger.warning(f"Could not init Flows client: {e}. Falling back to transfer_only.")

    # ---- Grouping Logic ----

    def _group_key(self, hm: HighMagModel) -> str:
        """Return a grouping key for a HighMagModel based on the configured grouping."""
        if self.cmd_data.grouping == 'per_micrograph':
            return str(hm.pk)
        elif self.cmd_data.grouping == 'per_group':
            # BIS group: all holes sharing the same group identifier
            return str(getattr(hm, 'bis_group', hm.pk))
        elif self.cmd_data.grouping == 'per_square':
            # All holes in the same square
            return str(hm.hole_id.square_id.pk) if hm.hole_id else str(hm.pk)
        return str(hm.pk)

    def _group_is_complete(self, group_key: str, group_items: List) -> bool:
        """Check if all items in a group have been acquired (ready to submit)."""
        if self.cmd_data.grouping == 'per_micrograph':
            return True  # always ready
        # For per_group and per_square, check if all expected items are acquired
        # TODO: Compare against expected count from parent model
        return all(hm.status == 'acquired' for hm in group_items)

    def _build_groups(self) -> Dict[str, List]:
        """Group incomplete processes by grouping key."""
        groups = {}
        for hm in self.incomplete_processes:
            key = self._group_key(hm)
            groups.setdefault(key, []).append(hm)
        return groups

    # ---- Main loop ----

    def start(self):
        if not self.project_path:
            logger.info(f'Grid {self.grid.grid_id} (slot {self.grid_position}): '
                        f'no Doppio project assigned, skipping.')
            return

        logger.info(f'Starting Doppio pipeline: mode={self.cmd_data.mode}, '
                     f'grouping={self.cmd_data.grouping}, '
                     f'project={self.project_path}, grid={self.grid.grid_id}')
        self._init_globus_clients()

        logger.info(f'Entering main loop. Stop={self._stop.is_set()}, stop_file={self.is_stop_file()}')
        # DEBUG: single-shot mode — submit one flow and exit
        DEBUG_SINGLE_SHOT = True

        while not self._stop.is_set() and not self.is_stop_file():
            self.list_incomplete_processes()
            logger.debug(f'Incomplete: {len(self.incomplete_processes)} images')

            # Group and submit ready batches (limit concurrent flow runs)
            MAX_CONCURRENT_FLOWS = 1
            groups = self._build_groups()
            for group_key, batch in groups.items():
                if len(self._active_flow_runs) >= MAX_CONCURRENT_FLOWS:
                    logger.debug(f'Max concurrent flows ({MAX_CONCURRENT_FLOWS}) reached, waiting...')
                    break
                if group_key in self._submitted_groups:
                    continue
                if self._group_is_complete(group_key, batch):
                    try:
                        self._submit_group(group_key, batch)
                        if DEBUG_SINGLE_SHOT:
                            logger.info('DEBUG: Single-shot mode — submitted one group, exiting loop.')
                            return
                    except Exception as e:
                        logger.error(f'Failed to submit group {group_key}: {e}')
                        break  # Stop submitting on error, retry next loop

            # Check for completed flow runs
            self._check_flow_runs()

            # Poll for Doppio results (.done.json files on HPC)
            self._poll_for_results()

            # Done?
            self.grid.refresh_from_db()
            if self._is_done():
                logger.info('All images processed. Exiting.')
                break

            time.sleep(5)  # brief sleep between loop iterations

    def _is_done(self):
        return (
            self.grid.status in ['complete', 'error']
            and len(self._active_flow_runs) == 0
            and not self.incomplete_processes
        )

    # ---- Process listing ----

    def list_incomplete_processes(self):
        self.incomplete_processes = list(
            HighMagModel.parent_manager
            .filter(grid_id=self.grid.pk, status__in=['acquired', 'skipped'])
            .order_by('status', 'completion_time')
        )

    # ---- Submission ----

    def _submit_group(self, group_key: str, batch: List):
        """Submit a group of images as a Globus Flow run (or transfer-only)."""
        logger.info(f'Submitting group {group_key}: {len(batch)} images')
        self._submitted_groups.add(group_key)

        # Build file list from actual frame paths
        file_pairs = []
        for hm in batch:
            if not hm.frames:
                logger.warning(f'No frames file for {hm.pk}, skipping')
                continue
            # Full container path to the frame file
            container_path = str(self.frames_dir / hm.frames)

            src = _container_to_globus_path(container_path, self.cmd_data.source_base_path,
                                                self.container_frames_root)
            dst = _globus_dest_path(container_path, self.cmd_data.destination_base_path,
                                    self.project_path, self.grid.grid_id)
            file_pairs.append((src, dst))

        if self.cmd_data.mode == 'transfer_and_process' and self.fc:
            self._start_flow_run(group_key, batch, file_pairs)
        else:
            self._start_transfer_only(group_key, batch, file_pairs)

    def _build_manifest(self, group_key: str, batch: List, file_pairs: List,
                         flow_run_id: str = "") -> dict:
        """Build a Doppio-side manifest for this batch.

        The manifest tells Doppio's orchestrator (in SmartScope mode):
        - What movies were transferred (relative to Doppio project dir)
        - The flow_run_id to callback when processing is done
        - Any metadata overrides (pixel size, voltage, etc.)
        """
        batch_id = f"{self.grid.grid_id}_{group_key}"

        # Movie paths relative to Doppio project dir
        # (SmartScopeMode resolves them to absolute via project_dir)
        dest_globus_base = (f"{self.cmd_data.destination_base_path.rstrip('/')}/"
                            f"{self.project_path.strip('/')}")
        movies = []
        for _, dst in file_pairs:
            # dst is a Globus collection path like /CryoEM/Projects/Rori/Movies/grid/frame.tif
            # Strip the project prefix to get relative path like Movies/grid/frame.tif
            rel = dst
            if rel.startswith(dest_globus_base):
                rel = rel[len(dest_globus_base):].lstrip('/')
            movies.append(rel)

        # Build config dict matching doppio-live-worker's PipelineConfig
        fs_root = self.cmd_data.destination_filesystem_root.rstrip('/')
        dest_globus = (f"{self.cmd_data.destination_base_path.rstrip('/')}/"
                       f"{self.project_path.strip('/')}")
        project_fs = f"{fs_root}/{dest_globus.lstrip('/')}"
        output_dir = f"{project_fs}/LivePreprocess/job001"

        config = {
            "project_dir": project_fs,
            "output_dir": output_dir,
        }

        # Pixel size
        if self.cmd_data.pixel_size_override > 0:
            config["pixel_size"] = self.cmd_data.pixel_size_override
        elif hasattr(self.detector, 'pixel_size') and self.detector.pixel_size:
            config["pixel_size"] = float(self.detector.pixel_size)

        # Microscope settings
        if hasattr(self.microscope, 'voltage') and self.microscope.voltage:
            config["voltage"] = int(self.microscope.voltage)
        if hasattr(self.microscope, 'spherical_abberation') and self.microscope.spherical_abberation:
            config["cs"] = float(self.microscope.spherical_abberation)

        # Processing parameters from form
        if self.cmd_data.dose_per_frame > 0:
            config["dose_per_frame"] = self.cmd_data.dose_per_frame
        config["motioncor_binning"] = self.cmd_data.motioncor_binning
        config["motioncor_patches"] = self.cmd_data.motioncor_patches
        config["do_motioncor"] = self.cmd_data.do_motioncor
        config["do_ctf"] = self.cmd_data.do_ctf
        config["do_miffi"] = self.cmd_data.do_miffi
        config["do_picking"] = self.cmd_data.do_picking
        config["do_extraction"] = self.cmd_data.do_extraction
        config["picking_threshold"] = self.cmd_data.picking_threshold
        config["picking_model"] = self.cmd_data.picking_model
        config["extract_box_size"] = self.cmd_data.extract_box_size
        config["extract_downscale"] = self.cmd_data.extract_downscale

        return {
            "batch_id": batch_id,
            "flow_run_id": flow_run_id,
            "movies": movies,
            "config": config,
            "grid_id": self.grid.grid_id,
        }

    def _transfer_manifest(self, manifest: dict):
        """Transfer the manifest JSON to the Doppio project's Manifests/ dir on HPC."""
        from globus_sdk import TransferData

        batch_id = manifest["batch_id"]
        dest_base = (f"{self.cmd_data.destination_base_path.rstrip('/')}/"
                     f"{self.project_path.strip('/')}")

        # Write manifest to the frames directory (same Globus mount as the frames)
        manifests_dir = self.frames_dir / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        local_path = manifests_dir / f"{batch_id}.json"
        local_path.write_text(json.dumps(manifest, indent=2))

        src_manifest = _container_to_globus_path(
            str(local_path), self.cmd_data.source_base_path,
            self.container_frames_root
        )
        dst_manifest = (f"{dest_base}/LivePreprocess/job001/"
                        f"Manifests/{batch_id}.json")

        td = TransferData(
            source_endpoint=self.cmd_data.source_collection_id,
            destination_endpoint=self.cmd_data.destination_collection_id,
            label=f'Manifest {batch_id}',
        )
        td.add_item(src_manifest, dst_manifest)

        result = self.tc.submit_transfer(td)
        logger.info(f'Manifest transfer submitted: {result["task_id"]} '
                     f'for batch {batch_id}')

    def _start_flow_run(self, group_key: str, batch: List, file_pairs: List):
        """Start a Globus Flow run: transfer frames+manifest -> compute -> transfer back."""
        # Globus collection path (for transfers)
        dest_globus_dir = (f"{self.cmd_data.destination_base_path.rstrip('/')}/"
                           f"{self.project_path.strip('/')}")
        # HPC filesystem path (for compute function)
        fs_root = self.cmd_data.destination_filesystem_root.rstrip('/')
        dest_fs_dir = f"{fs_root}/{dest_globus_dir.lstrip('/')}"

        batch_id = f"{self.grid.grid_id}_{group_key}"
        manifest_globus_path = (f"{dest_globus_dir}/LivePreprocess/job001/"
                                f"Manifests/{batch_id}.json")
        manifest_path_on_hpc = (f"{dest_fs_dir}/LivePreprocess/job001/"
                                f"Manifests/{batch_id}.json")

        # Build manifest and write it to the frames directory (same Globus mount)
        manifest = self._build_manifest(group_key, batch, file_pairs)
        manifests_dir = self.frames_dir / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        local_path = manifests_dir / f"{batch_id}.json"
        local_path.write_text(json.dumps(manifest, indent=2))

        # Add manifest to the same transfer as the frames
        src_manifest = _container_to_globus_path(
            str(local_path), self.cmd_data.source_base_path,
            self.container_frames_root
        )
        all_items = [{"source_path": s, "destination_path": d} for s, d in file_pairs]
        all_items.append({"source_path": src_manifest, "destination_path": manifest_globus_path})

        flow_input = {
            "source_collection": self.cmd_data.source_collection_id,
            "destination_collection": self.cmd_data.destination_collection_id,
            "compute_endpoint": self.cmd_data.globus_compute_endpoint_id,
            "compute_function_id": self.cmd_data.compute_function_id,
            "compute_kwargs": {
                "manifest_path": manifest_path_on_hpc,
                "project_dir": dest_fs_dir,
                "source_collection": self.cmd_data.source_collection_id,
                "destination_collection": self.cmd_data.destination_collection_id,
                "source_base_path": self.cmd_data.source_base_path,
                "destination_base_path": self.cmd_data.destination_base_path,
                "destination_filesystem_root": self.cmd_data.destination_filesystem_root,
            },
            "transfer_items": all_items,
            "label": f"SmartScope {self.grid.grid_id} group {group_key}",
            "results_label": f"Results {self.grid.grid_id} group {group_key}",
        }

        run = self.fc.run_flow(body={"input": flow_input})
        run_id = run["run_id"]
        self._active_flow_runs[run_id] = {"batch": batch, "group_key": group_key}
        logger.info(f'Flow run started: {run_id} for group {group_key}')

    def _start_transfer_only(self, group_key: str, batch: List, file_pairs: List):
        """Transfer-only mode: just move files to HPC.

        Also writes a manifest so Doppio can pick them up later
        if the user starts a Live job manually.
        """
        from globus_sdk import TransferData

        td = TransferData(
            source_endpoint=self.cmd_data.source_collection_id,
            destination_endpoint=self.cmd_data.destination_collection_id,
            label=f'SmartScope->HPC {self.grid.grid_id} group {group_key}',
        )
        for src, dst in file_pairs:
            td.add_item(src, dst)

        result = self.tc.submit_transfer(td)
        task_id = result['task_id']
        self._active_flow_runs[task_id] = {"batch": batch, "group_key": group_key, "transfer_only": True}
        logger.info(f'Transfer submitted: {task_id} for group {group_key}')

        # Write manifest (no flow_run_id — no callback expected)
        manifest = self._build_manifest(group_key, batch, file_pairs)
        self._transfer_manifest(manifest)

    # ---- Flow run status ----

    def _check_flow_runs(self):
        """Check status of active flow runs / transfers."""
        for run_id, info in list(self._active_flow_runs.items()):
            if info.get("transfer_only"):
                self._check_transfer(run_id, info)
            else:
                self._check_flow(run_id, info)

    def _check_transfer(self, task_id: str, info: dict):
        task = self.tc.get_task(task_id)
        if task['status'] == 'SUCCEEDED':
            logger.info(f'Transfer {task_id} completed for group {info["group_key"]}')
            del self._active_flow_runs[task_id]
        elif task['status'] == 'FAILED':
            logger.error(f'Transfer {task_id} failed: {task.get("nice_status_details", "")}')
            del self._active_flow_runs[task_id]
            self._submitted_groups.discard(info["group_key"])

    def _check_flow(self, run_id: str, info: dict):
        run = self.fc_general.get_run(run_id)
        status = run["status"]

        if status == "SUCCEEDED":
            logger.info(f'Flow run {run_id} completed for group {info["group_key"]}')
            self._update_db(info["batch"], run.get("details", {}))
            del self._active_flow_runs[run_id]

        elif status in ("FAILED", "CANCELLED"):
            logger.error(f'Flow run {run_id} {status} for group {info["group_key"]}')
            del self._active_flow_runs[run_id]
            self._submitted_groups.discard(info["group_key"])

    # ---- Results polling & DB updates ----

    def _poll_for_results(self):
        """Check HPC for .done.json files, transfer thumbnails back, update DB."""
        if not hasattr(self, '_polled_done_files'):
            self._polled_done_files = set()

        # List the Manifests/ dir on HPC via Globus for .done.json files
        dest_globus_dir = (f"{self.cmd_data.destination_base_path.rstrip('/')}/"
                           f"{self.project_path.strip('/')}")
        manifests_globus = f"{dest_globus_dir}/LivePreprocess/job001/Manifests"

        try:
            entries = list(self.tc.operation_ls(
                self.cmd_data.destination_collection_id,
                path=manifests_globus,
            ))
        except Exception as e:
            logger.debug(f'Could not list Manifests dir: {e}')
            return

        for entry in entries:
            name = entry['name']
            if not name.endswith('.done.json'):
                continue
            if name in self._polled_done_files:
                continue

            self._polled_done_files.add(name)
            logger.info(f'Found completion marker: {name}')

            # Transfer the .done.json back
            self._transfer_done_file_and_update(name, manifests_globus, dest_globus_dir)

    def _transfer_done_file_and_update(self, done_filename: str,
                                        manifests_globus: str, dest_globus_dir: str):
        """Transfer .done.json + thumbnails from HPC, then update DB."""
        from globus_sdk import TransferData
        import time as _time

        # Transfer .done.json to local manifests dir
        local_manifests = self.frames_dir / "manifests"
        local_manifests.mkdir(parents=True, exist_ok=True)
        local_done_path = local_manifests / done_filename

        src_done = f"{manifests_globus}/{done_filename}"
        dst_done = _container_to_globus_path(
            str(local_done_path), self.cmd_data.source_base_path,
            self.container_frames_root
        )

        td = TransferData(
            source_endpoint=self.cmd_data.destination_collection_id,
            destination_endpoint=self.cmd_data.source_collection_id,
            label=f'Results {done_filename}',
        )
        td.add_item(src_done, dst_done)

        # Also transfer thumbnails referenced in the done file
        # We'll add them after we read the done file — for now just get the done file
        try:
            result = self.tc.submit_transfer(td)
            task_id = result['task_id']
            logger.info(f'Results transfer submitted: {task_id}')

            # Wait for this small transfer to complete
            for _ in range(30):
                task = self.tc.get_task(task_id)
                if task['status'] == 'SUCCEEDED':
                    break
                elif task['status'] in ('FAILED', 'CANCELLED'):
                    logger.error(f'Results transfer failed: {task_id}')
                    return
                _time.sleep(2)

        except Exception as e:
            logger.error(f'Failed to transfer results: {e}')
            return

        # Read the .done.json and update DB
        if not local_done_path.exists():
            logger.warning(f'Done file not found locally after transfer: {local_done_path}')
            return

        done_data = json.loads(local_done_path.read_text())
        if done_data.get('status') != 'completed':
            logger.warning(f'Batch {done_data.get("batch_id")} failed: {done_data.get("error", "")}')
            return

        # Now transfer thumbnails back
        self._transfer_thumbnails(done_data, dest_globus_dir)

        # Update DB with results
        self._update_db_from_done(done_data)

    def _transfer_thumbnails(self, done_data: dict, dest_globus_dir: str):
        """Transfer micrograph + CTF thumbnails from HPC back to DTN."""
        from globus_sdk import TransferData
        import time as _time

        td = TransferData(
            source_endpoint=self.cmd_data.destination_collection_id,
            destination_endpoint=self.cmd_data.source_collection_id,
            label=f'Thumbnails {done_data.get("batch_id", "")}',
        )

        results = done_data.get('results', [])
        item_count = 0
        for mic in results:
            # Micrograph thumbnail
            thumb = mic.get('thumbnail', '')
            if thumb:
                src = f"{dest_globus_dir}/{thumb.lstrip('/')}"
                # Put thumbnail in SmartScope's pngs/ directory
                movie_stem = Path(mic.get('movie', '')).stem
                hm_name = self._find_hm_name_for_movie(movie_stem)
                if hm_name:
                    dst_local = Path(self.grid.directory) / 'pngs' / f'{hm_name}.png'
                    dst_local.parent.mkdir(parents=True, exist_ok=True)
                    dst = _container_to_globus_path(
                        str(dst_local), self.cmd_data.source_base_path,
                        str(Path(self.grid.directory).parents[1])
                    )
                    td.add_item(src, dst)
                    item_count += 1

            # CTF thumbnail
            ctf_thumb = mic.get('ctf_thumbnail', '')
            if ctf_thumb:
                src = f"{dest_globus_dir}/{ctf_thumb.lstrip('/')}"
                if hm_name:
                    dst_local = Path(self.grid.directory) / hm_name / 'ctf.png'
                    dst_local.parent.mkdir(parents=True, exist_ok=True)
                    dst = _container_to_globus_path(
                        str(dst_local), self.cmd_data.source_base_path,
                        str(Path(self.grid.directory).parents[1])
                    )
                    td.add_item(src, dst)
                    item_count += 1

        if item_count == 0:
            logger.debug('No thumbnails to transfer')
            return

        try:
            result = self.tc.submit_transfer(td)
            logger.info(f'Thumbnail transfer submitted: {result["task_id"]} ({item_count} files)')
        except Exception as e:
            logger.error(f'Failed to submit thumbnail transfer: {e}')

    def _find_hm_name_for_movie(self, movie_stem: str) -> str:
        """Find the HighMagModel name that corresponds to a movie filename stem."""
        for hm in self.incomplete_processes:
            if hm.frames and Path(hm.frames).stem == movie_stem:
                return hm.name
        return ''

    def _update_db_from_done(self, done_data: dict):
        """Update HighMagModel + HoleModel from a .done.json file."""
        from django.utils import timezone

        results = done_data.get('results', [])
        pixel_size = self.cmd_data.pixel_size_override or (
            float(self.detector.pixel_size) if hasattr(self.detector, 'pixel_size')
            and self.detector.pixel_size else 1.0
        )

        highmags_to_update = []
        holes_to_update = []

        for mic in results:
            movie_stem = Path(mic.get('movie', '')).stem
            hm_name = self._find_hm_name_for_movie(movie_stem)
            if not hm_name:
                logger.warning(f'No HighMagModel found for movie {movie_stem}')
                continue

            try:
                hm = HighMagModel.objects.get(name=hm_name)
            except HighMagModel.DoesNotExist:
                logger.warning(f'HighMagModel {hm_name} not in DB')
                continue

            defocus_u = mic.get('defocus_u', 0.0)
            defocus_v = mic.get('defocus_v', 0.0)

            hm.defocus = (defocus_u + defocus_v) / 2.0
            hm.astig = abs(defocus_u - defocus_v)
            hm.angast = mic.get('defocus_angle', 0.0)
            hm.ctffit = mic.get('ctf_max_resolution', 999.0)
            hm.ice_thickness = int(round(mic.get('ice_thickness', 0.0) / 10))
            hm.shape_x = mic.get('shape_x', 0)
            hm.shape_y = mic.get('shape_y', 0)
            hm.pixel_size = pixel_size
            hm.status = 'completed'
            hm.completion_time = timezone.now()
            highmags_to_update.append(hm)

            if hm.hole_id:
                hm.hole_id.status = 'completed'
                hm.hole_id.completion_time = timezone.now()
                holes_to_update.append(hm.hole_id)

        if highmags_to_update or holes_to_update:
            with transaction.atomic():
                if highmags_to_update:
                    HighMagModel.objects.bulk_update(
                        highmags_to_update,
                        fields=['status', 'defocus', 'astig', 'angast', 'ctffit',
                                'ice_thickness', 'shape_x', 'shape_y', 'pixel_size',
                                'completion_time']
                    )
                if holes_to_update:
                    HoleModel.objects.bulk_update(
                        holes_to_update,
                        fields=['status', 'completion_time']
                    )

            all_updated = highmags_to_update + holes_to_update
            websocket_update(all_updated, self.grid.grid_id)
            logger.info(f"Updated {len(highmags_to_update)} high-mag images, "
                         f"{len(holes_to_update)} holes from Doppio results")

    def check_for_update(self, instance):
        pass  # Handled by _poll_for_results

    # ---- Shutdown ----

    def stop(self):
        logger.info('Stopping Doppio preprocessing pipeline')
        self._stop.set()


# ---- CLI entry point for login ----

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        client_id = sys.argv[2] if len(sys.argv) > 2 else "7df9d534-fb19-4d79-8e83-642f1cdcf081"
        token_file = sys.argv[3] if len(sys.argv) > 3 else "/opt/config/smartscope_tokens.json"
        from doppio_cmd_kwargs import DoppioCmdKwargs  # noqa
        # Inline login — see globus_login.py for the two-phase flow
        print("Use globus_login.py for interactive login.")
    else:
        print("Usage: python -m Smartscope.core.pipelines.doppio_preprocessing_pipeline login")
