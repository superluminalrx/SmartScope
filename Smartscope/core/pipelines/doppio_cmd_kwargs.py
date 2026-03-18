
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)


class DoppioCmdKwargs(BaseModel):
    mode: str = "transfer_and_process"  # "transfer_only" | "transfer_and_process"

    # Globus Compute endpoint where Doppio jobs run
    globus_compute_endpoint_id: str = ""

    # Globus Transfer collection IDs
    source_collection_id: str = ""
    destination_collection_id: str = ""

    # Paths on each side
    source_base_path: str = ""
    destination_base_path: str = ""

    # Frames
    frames_directory: str = ""

    # Globus Auth
    globus_client_id: str = ""
    globus_client_secret_file: str = ""

    # Tuning
    poll_interval: float = 5.0
    batch_size: int = 10
