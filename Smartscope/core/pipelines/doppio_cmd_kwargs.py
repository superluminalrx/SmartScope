
from typing import Dict
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)


class DoppioCmdKwargs(BaseModel):
    mode: str = "transfer_and_process"  # "transfer_only" | "transfer_and_process"

    # How to group images into flow runs
    #   per_micrograph: one flow per high-mag image (immediate)
    #   per_group: one flow per BIS group
    #   per_square: one flow per grid square (wait until square is done)
    grouping: str = "per_group"

    # Globus Compute endpoint where Doppio jobs run
    globus_compute_endpoint_id: str = ""

    # Registered Globus Compute function ID for Doppio processing
    compute_function_id: str = "62937010-4730-4ecc-8840-e198a91672d9"

    # Globus Flow ID (deployed flow that chains transfer -> compute -> transfer)
    globus_flow_id: str = ""

    # Globus Transfer collection IDs
    source_collection_id: str = ""
    destination_collection_id: str = ""

    # Path mapping
    source_base_path: str = ""
    destination_base_path: str = ""

    # ===== Slot-to-project mapping (autoloader positions 1-12) =====
    slot_1: str = ""
    slot_2: str = ""
    slot_3: str = ""
    slot_4: str = ""
    slot_5: str = ""
    slot_6: str = ""
    slot_7: str = ""
    slot_8: str = ""
    slot_9: str = ""
    slot_10: str = ""
    slot_11: str = ""
    slot_12: str = ""

    # ===== Pipeline stages (checkboxes) =====
    do_motioncor: bool = True
    do_ctf: bool = True
    do_miffi: bool = False
    do_picking: bool = True
    do_extraction: bool = True

    # ===== Processing parameters =====
    # These are user-provided — can't be derived from microscope metadata
    pixel_size_override: float = 0.0  # 0 = use SmartScope value
    dose_per_frame: float = 1.0
    box_size: int = 200
    motioncor_binning: float = 1.0
    motioncor_patches: int = 5
    picking_threshold: float = 0.3
    picking_model: str = ""  # path to crYOLO model on HPC, empty = general model
    extract_box_size: int = 256
    extract_downscale: int = 1

    # ===== Globus Auth =====
    globus_client_id: str = "7df9d534-fb19-4d79-8e83-642f1cdcf081"
    token_file: str = "/opt/config/smartscope_tokens.json"

    def get_project_for_slot(self, position: int) -> str:
        """Return the Doppio project path for a given autoloader slot (1-12)."""
        return getattr(self, f"slot_{position}", "")
