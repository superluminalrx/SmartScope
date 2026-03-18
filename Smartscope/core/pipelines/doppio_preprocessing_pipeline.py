
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


class DoppioPreprocessingPipeline(PreprocessingPipeline):

    verbose_name = 'Doppio Preprocessing Pipeline (Globus)'
    name = 'doppioPipeline'
    description = 'GPU preprocessing via Globus Compute + Transfer to HPC running Doppio.'

    cmdkwargs_handler = DoppioCmdKwargs
    pipeline_form = DoppioPipelineForm

    incomplete_processes: List = []
    to_update: List = []

    def __init__(self, grid: AutoloaderGrid, cmd_data: Dict):
        super().__init__(grid=grid)
        self.microscope = self.grid.session_id.microscope_id
        self.detector = self.grid.session_id.detector_id
        self.cmd_data = self.cmdkwargs_handler.parse_obj(cmd_data)

        self._stop = threading.Event()
        self._submitted: Set[str] = set()
        self._pending_transfers: Dict[str, List] = {}   # transfer_task_id → batch
        self._pending_compute: Dict[str, List] = {}     # compute_task_id → batch

        self.tc = None   # globus_sdk.TransferClient
        self.gcc = None  # globus_compute_sdk.Executor

    # ---- Globus client initialization ----

    def _init_globus_clients(self):
        """Initialize Globus Transfer and Compute clients from credentials."""
        from globus_sdk import ConfidentialAppAuthClient, TransferClient, ClientCredentialsAuthorizer
        from globus_compute_sdk import Executor

        secret = Path(self.cmd_data.globus_client_secret_file).read_text().strip()
        auth_client = ConfidentialAppAuthClient(self.cmd_data.globus_client_id, secret)

        # Transfer client
        transfer_scope = "urn:globus:auth:scope:transfer.api.globus.org:all"
        cc_authorizer = ClientCredentialsAuthorizer(auth_client, transfer_scope)
        self.tc = TransferClient(authorizer=cc_authorizer)

        # Compute client
        self.gcc = Executor(
            endpoint_id=self.cmd_data.globus_compute_endpoint_id,
            funcx_client_id=self.cmd_data.globus_client_id,
            funcx_client_secret=secret,
        )

    # ---- Main loop ----

    def start(self):
        logger.info(f'Starting Doppio preprocessing pipeline in mode: {self.cmd_data.mode}')
        self._init_globus_clients()

        while not self._stop.is_set() and not self.is_stop_file():
            self.list_incomplete_processes()

            # Batch and submit new work
            unsubmitted = [p for p in self.incomplete_processes if p.pk not in self._submitted]
            if unsubmitted:
                batch = unsubmitted[:self.cmd_data.batch_size]
                self._submit_batch(batch)

            # Poll for completions
            self._poll_pending_transfers()
            self._poll_pending_compute()

            # Check if we're done
            self.grid.refresh_from_db()
            if self._is_done():
                logger.info('All images processed. Exiting.')
                break

            time.sleep(self.cmd_data.poll_interval)

    def _is_done(self):
        return (
            self.grid.status in ['complete', 'error']
            and len(self._pending_transfers) == 0
            and len(self._pending_compute) == 0
            and not any(p.status == 'acquired' for p in self.incomplete_processes)
        )

    # ---- Process listing ----

    def list_incomplete_processes(self):
        self.incomplete_processes = list(
            HighMagModel.parent_manager
            .filter(grid_id=self.grid.pk, status__in=['acquired', 'skipped'])
            .order_by('status', 'completion_time')
        )

    # ---- Batch submission ----

    def _submit_batch(self, batch: List):
        """Transfer frames to HPC, optionally submit Doppio compute job."""
        logger.info(f'Submitting batch of {len(batch)} images')
        self._submitted.update(p.pk for p in batch)

        task_id = self._transfer_to_hpc(batch)
        self._pending_transfers[task_id] = batch

    # ---- Globus Transfer: to HPC ----

    def _transfer_to_hpc(self, batch: List) -> str:
        """Submit a Globus Transfer task to move frames to HPC. Returns task_id."""
        from globus_sdk import TransferData

        transfer_data = TransferData(
            self.tc,
            self.cmd_data.source_collection_id,
            self.cmd_data.destination_collection_id,
            label=f'SmartScope→HPC {self.grid.grid_id}',
        )

        for hm in batch:
            source_path = self._source_path_for(hm)
            dest_path = self._dest_path_for(hm)
            transfer_data.add_item(source_path, dest_path)

        result = self.tc.submit_transfer(transfer_data)
        task_id = result['task_id']
        logger.info(f'Globus Transfer submitted: {task_id}')
        return task_id

    def _source_path_for(self, hm: HighMagModel) -> str:
        """Build the source path for a HighMagModel's frames on the local collection."""
        # TODO: Resolve actual frame file path from hm.frames and frames_directory
        raise NotImplementedError

    def _dest_path_for(self, hm: HighMagModel) -> str:
        """Build the destination path for a HighMagModel's frames on the HPC collection."""
        # TODO: Map to destination_base_path / grid_id / frame_filename
        raise NotImplementedError

    # ---- Globus Transfer: from HPC ----

    def _transfer_from_hpc(self, batch: List, result_paths: Dict) -> str:
        """Transfer Doppio results back from HPC. Returns task_id."""
        from globus_sdk import TransferData

        transfer_data = TransferData(
            self.tc,
            self.cmd_data.destination_collection_id,
            self.cmd_data.source_collection_id,
            label=f'HPC→SmartScope {self.grid.grid_id}',
        )

        for hm in batch:
            # TODO: Map result files (motion-corrected avg, CTF) to local paths
            pass

        result = self.tc.submit_transfer(transfer_data)
        task_id = result['task_id']
        logger.info(f'Globus Transfer (results) submitted: {task_id}')
        return task_id

    # ---- Globus Compute: Doppio job ----

    def _submit_doppio_job(self, batch: List) -> str:
        """Submit a Doppio processing job via Globus Compute. Returns task_id."""
        # TODO: Define the function to submit and the input payload
        # The function should:
        #   - Take a path to frames on HPC
        #   - Run Doppio processing (motion correction, CTF estimation)
        #   - Return a dict of results or a path to output files
        raise NotImplementedError

    # ---- Polling ----

    def _poll_pending_transfers(self):
        """Check status of pending Globus Transfer tasks."""
        for task_id, batch in list(self._pending_transfers.items()):
            task = self.tc.get_task(task_id)
            if task['status'] == 'SUCCEEDED':
                logger.info(f'Transfer {task_id} completed')
                del self._pending_transfers[task_id]

                if self.cmd_data.mode == 'transfer_and_process':
                    compute_id = self._submit_doppio_job(batch)
                    self._pending_compute[compute_id] = batch

            elif task['status'] == 'FAILED':
                logger.error(f'Transfer {task_id} failed: {task.get("nice_status_details", "")}')
                del self._pending_transfers[task_id]
                # Allow retry on next loop
                self._submitted -= {p.pk for p in batch}

    def _poll_pending_compute(self):
        """Check status of pending Globus Compute tasks."""
        for task_id, batch in list(self._pending_compute.items()):
            # TODO: Check task status via Globus Compute SDK
            # On success:
            #   result = self.gcc.get_result(task_id)
            #   self._transfer_from_hpc(batch, result)
            #   self._update_db(batch, result)
            #   del self._pending_compute[task_id]
            pass

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
        # TODO: Cancel pending Globus transfers and compute tasks
        if self.gcc:
            self.gcc.shutdown()
