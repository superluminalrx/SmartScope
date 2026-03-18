
import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Set

from django.db import transaction

from Smartscope.core.db_manipulations import websocket_update
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

# Container data mount — strip this prefix when building Globus paths
CONTAINER_DATA_ROOT = "/mnt/data"


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
    # TODO: Flows tokens may be under a different resource server key
    # depending on how scopes were requested at login time
    flow_tokens = tokens.get("flows.globus.org", tokens.get("transfer.api.globus.org"))

    auth_client = NativeAppAuthClient(cmd_data.globus_client_id)
    authorizer = RefreshTokenAuthorizer(
        flow_tokens["refresh_token"],
        auth_client,
        access_token=flow_tokens["access_token"],
        expires_at=flow_tokens["expires_at_seconds"],
    )
    return SpecificFlowClient(cmd_data.globus_flow_id, authorizer=authorizer)


# ---- Path Mapping ----

def _container_to_globus_path(container_path: str, source_base_path: str) -> str:
    """Convert a container path to a Globus collection path.

    e.g. /mnt/data/Superluminal/session/movies/frame.tif
      -> /SmartScope/Superluminal/session/movies/frame.tif
    """
    rel = container_path
    if rel.startswith(CONTAINER_DATA_ROOT):
        rel = rel[len(CONTAINER_DATA_ROOT):]
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

        self._stop = threading.Event()
        self._submitted_groups: Set[str] = set()  # track submitted grouping keys
        self._active_flow_runs: Dict[str, dict] = {}  # flow_run_id -> {batch, group_key}

        self.tc = None       # TransferClient (for transfer_only mode)
        self.fc = None       # SpecificFlowClient (for transfer_and_process mode)

    # ---- Init ----

    def _init_globus_clients(self):
        self.tc = _build_transfer_client(self.cmd_data)
        logger.info("Globus Transfer client initialized")

        if self.cmd_data.mode == 'transfer_and_process' and self.cmd_data.globus_flow_id:
            try:
                self.fc = _build_flows_client(self.cmd_data)
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

        while not self._stop.is_set() and not self.is_stop_file():
            self.list_incomplete_processes()

            # Group and submit ready batches
            groups = self._build_groups()
            for group_key, batch in groups.items():
                if group_key in self._submitted_groups:
                    continue
                if self._group_is_complete(group_key, batch):
                    self._submit_group(group_key, batch)

            # Check for completed flow runs
            self._check_flow_runs()

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

        # Build file list
        file_pairs = []
        for hm in batch:
            # TODO: Get the actual frame file path from the HighMagModel
            # container_path = hm.get_frame_path() or similar
            # For now, placeholder:
            container_path = f"{CONTAINER_DATA_ROOT}/{hm.pk}"

            src = _container_to_globus_path(container_path, self.cmd_data.source_base_path)
            dst = _globus_dest_path(container_path, self.cmd_data.destination_base_path,
                                    self.project_path, self.grid.grid_id)
            file_pairs.append((src, dst))

        if self.cmd_data.mode == 'transfer_and_process' and self.fc:
            self._start_flow_run(group_key, batch, file_pairs)
        else:
            self._start_transfer_only(group_key, batch, file_pairs)

    def _start_flow_run(self, group_key: str, batch: List, file_pairs: List):
        """Start a Globus Flow run: transfer -> compute -> transfer back."""
        flow_input = {
            "source_collection": self.cmd_data.source_collection_id,
            "destination_collection": self.cmd_data.destination_collection_id,
            "compute_endpoint": self.cmd_data.globus_compute_endpoint_id,
            "transfer_items": [{"source": s, "destination": d} for s, d in file_pairs],
            "label": f"SmartScope {self.grid.grid_id} group {group_key}",
        }

        run = self.fc.run_flow(body={"input": flow_input})
        run_id = run["run_id"]
        self._active_flow_runs[run_id] = {"batch": batch, "group_key": group_key}
        logger.info(f'Flow run started: {run_id} for group {group_key}')

    def _start_transfer_only(self, group_key: str, batch: List, file_pairs: List):
        """Transfer-only mode: just move files to HPC."""
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
        run = self.fc.get_run(run_id)
        status = run["status"]

        if status == "SUCCEEDED":
            logger.info(f'Flow run {run_id} completed for group {info["group_key"]}')
            self._update_db(info["batch"], run.get("details", {}))
            del self._active_flow_runs[run_id]

        elif status in ("FAILED", "CANCELLED"):
            logger.error(f'Flow run {run_id} {status} for group {info["group_key"]}')
            del self._active_flow_runs[run_id]
            self._submitted_groups.discard(info["group_key"])

    # ---- DB updates ----

    def _update_db(self, batch: List, results: Dict):
        """Parse Doppio output and update HighMagModel + HoleModel in DB."""
        to_update = []

        for hm in batch:
            # TODO: Extract per-image results from Doppio output
            # data = {
            #     'defocus': ...,
            #     'astig': ...,
            #     'angast': ...,
            #     'ctffit': ...,
            #     'shape_x': ...,
            #     'shape_y': ...,
            #     'pixel_size': ...,
            #     'status': 'completed',
            # }
            # to_update.append(update_fields(hm, data))
            # to_update.append(update_fields(hm.hole_id, dict(status='completed')))
            pass

        if to_update:
            with transaction.atomic():
                highmags = [x for x in to_update if isinstance(x, HighMagModel)]
                holes = [x for x in to_update if isinstance(x, HoleModel)]
                HighMagModel.objects.bulk_update(
                    highmags,
                    fields=['status', 'shape_x', 'shape_y', 'pixel_size',
                            'defocus', 'astig', 'angast', 'ctffit',
                            'tilt_angle', 'tilt_axis_angle', 'ice_thickness',
                            'completion_time']
                )
                HoleModel.objects.bulk_update(holes, fields=['status', 'completion_time'])
            websocket_update(to_update, self.grid.grid_id)

    def check_for_update(self, instance):
        pass  # Handled by _update_db

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
