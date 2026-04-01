
from django import forms
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# Default token file and client ID
DEFAULT_TOKEN_FILE = '/opt/config/smartscope_tokens.json'
DEFAULT_CLIENT_ID = '7df9d534-fb19-4d79-8e83-642f1cdcf081'


def _get_globus_choices():
    """Query Globus APIs for available collections, flows, and compute endpoints.

    Returns a dict of field_name -> [(value, label), ...] choices.
    Falls back to empty lists if tokens are missing or API fails.
    """
    choices = {
        'source_collection_id': [],
        'destination_collection_id': [],
        'globus_flow_id': [],
        'globus_compute_endpoint_id': [],
        'globus_compute_function_id': [],
    }

    try:
        from globus_sdk import (
            NativeAppAuthClient, TransferClient, FlowsClient,
            RefreshTokenAuthorizer,
        )

        token_path = Path(DEFAULT_TOKEN_FILE)
        if not token_path.exists():
            return choices

        tokens = json.loads(token_path.read_text())
        auth_client = NativeAppAuthClient(DEFAULT_CLIENT_ID)

        # Transfer collections
        if 'transfer.api.globus.org' in tokens:
            t = tokens['transfer.api.globus.org']
            authorizer = RefreshTokenAuthorizer(
                t['refresh_token'], auth_client,
                access_token=t['access_token'],
                expires_at=t['expires_at_seconds'],
            )
            tc = TransferClient(authorizer=authorizer)

            collection_choices = []
            for ep in tc.endpoint_search(filter_scope='my-endpoints'):
                label = f"{ep['display_name']}  ({ep['id'][:8]}…)"
                collection_choices.append((ep['id'], label))
            # Also search recently used
            for ep in tc.endpoint_search(filter_scope='recently-used'):
                entry = (ep['id'], f"{ep['display_name']}  ({ep['id'][:8]}…)")
                if entry not in collection_choices:
                    collection_choices.append(entry)

            choices['source_collection_id'] = collection_choices
            choices['destination_collection_id'] = collection_choices

        # Flows
        if 'flows.globus.org' in tokens:
            t = tokens['flows.globus.org']
            authorizer = RefreshTokenAuthorizer(
                t['refresh_token'], auth_client,
                access_token=t['access_token'],
                expires_at=t['expires_at_seconds'],
            )
            fc = FlowsClient(authorizer=authorizer)

            flow_choices = []
            for flow in fc.list_flows():
                label = f"{flow['title']}  ({flow['id'][:8]}…)"
                flow_choices.append((flow['id'], label))
            choices['globus_flow_id'] = flow_choices

        # Compute functions
        if 'funcx_service' in tokens:
            try:
                from globus_sdk import ComputeClientV2
                t = tokens['funcx_service']
                authorizer = RefreshTokenAuthorizer(
                    t['refresh_token'], auth_client,
                    access_token=t['access_token'],
                    expires_at=t['expires_at_seconds'],
                )
                cc = ComputeClientV2(authorizer=authorizer)
                func_choices = []
                for func in cc.get('/v3/functions').data.get('functions', []):
                    name = func.get('name', func['function_uuid'][:8])
                    label = f"{name}  ({func['function_uuid'][:8]}…)"
                    func_choices.append((func['function_uuid'], label))
                choices['globus_compute_function_id'] = func_choices
            except Exception as e:
                logger.debug(f'Could not list compute functions: {e}')

    except Exception as e:
        logger.warning(f'Could not fetch Globus choices: {e}')

    return choices


class GlobusPipelineForm(forms.Form):

    grouping = forms.ChoiceField(
        choices=[
            ('per_micrograph', 'Per Micrograph — one flow per image (immediate)'),
            ('per_group', 'Per Group — one flow per BIS group'),
            ('per_square', 'Per Square — one flow per grid square'),
        ],
        widget=forms.RadioSelect(attrs={'class': ''}),
        initial='per_group',
        help_text='How to batch images into Globus Flow runs.',
    )

    max_concurrent_flows = forms.IntegerField(
        label='Max concurrent flows',
        initial=1,
        min_value=1,
        max_value=20,
        help_text='Maximum number of Globus Flow runs to have active simultaneously.',
    )

    # ===== Globus — these become dropdowns populated from the API =====

    globus_compute_endpoint_id = forms.CharField(
        label='Globus Compute Endpoint',
        widget=forms.TextInput(attrs={'placeholder': 'Run: globus-compute-endpoint list (on HPC)'}),
        help_text='UUID from "globus-compute-endpoint list" on the HPC.',
    )

    globus_compute_function_id = forms.ChoiceField(
        label='Compute Function',
        choices=[],
        help_text='The processing function to run on the HPC. Register new functions with register_doppio_function.py.',
    )

    globus_flow_id = forms.ChoiceField(
        label='Globus Flow',
        choices=[],
        help_text='Select a deployed Globus Flow.',
    )

    source_collection_id = forms.ChoiceField(
        label='Source Collection',
        choices=[],
        help_text='Globus collection on the microscope side.',
    )

    destination_collection_id = forms.ChoiceField(
        label='Destination Collection',
        choices=[],
        help_text='Globus collection on the HPC side.',
    )

    source_base_path = forms.CharField(
        label='Source Base Path',
        help_text='Globus path prefix mapping to the container data root. '
                  'e.g. "/SmartScope" if that maps to /mnt/data inside the container.',
    )

    destination_base_path = forms.CharField(
        label='Destination Base Path (Globus)',
        help_text='Globus collection path for Doppio projects, e.g. "/CryoEM/Projects".',
    )

    destination_filesystem_root = forms.CharField(
        label='Destination Filesystem Root (HPC)',
        help_text='HPC filesystem mount point for the Globus collection root, e.g. "/mnt/blackmore/ext-superluminal".',
    )

    # ===== Slot-to-Project Mapping =====

    slot_1 = forms.CharField(
        label='Slot 1',
        help_text='Doppio project path. e.g. "Lodos/Apoferritin". Same value = same project. Empty = skip.',
    )
    slot_2 = forms.CharField(label='Slot 2')
    slot_3 = forms.CharField(label='Slot 3')
    slot_4 = forms.CharField(label='Slot 4')
    slot_5 = forms.CharField(label='Slot 5')
    slot_6 = forms.CharField(label='Slot 6')
    slot_7 = forms.CharField(label='Slot 7')
    slot_8 = forms.CharField(label='Slot 8')
    slot_9 = forms.CharField(label='Slot 9')
    slot_10 = forms.CharField(label='Slot 10')
    slot_11 = forms.CharField(label='Slot 11')
    slot_12 = forms.CharField(label='Slot 12')

    # ===== Pipeline Stages =====

    do_motioncor = forms.BooleanField(
        label='Motion Correction',
        initial=True,
        help_text='Run motion correction (MotionCor3).',
    )
    do_ctf = forms.BooleanField(
        label='CTF Estimation',
        initial=True,
        help_text='Run CTF estimation (CTFFind5).',
    )
    do_miffi = forms.BooleanField(
        label='MiFFI Filtering',
        initial=False,
        help_text='Run MiFFI CNN micrograph quality classification.',
    )
    do_picking = forms.BooleanField(
        label='Particle Picking',
        initial=True,
        help_text='Run particle picking (crYOLO).',
    )
    do_extraction = forms.BooleanField(
        label='Particle Extraction',
        initial=True,
        help_text='Extract particles after picking.',
    )

    # ===== Processing Parameters =====

    pixel_size_override = forms.FloatField(
        label='Pixel size override (A/px)',
        initial=0.0,
        help_text='Override pixel size from microscope metadata. 0 = use SmartScope value.',
    )

    dose_per_frame = forms.FloatField(
        label='Dose per frame (e/A²)',
        initial=1.0,
        help_text='Electron dose per frame for dose weighting.',
    )

    box_size = forms.IntegerField(
        label='Picking box size (px)',
        initial=200,
        help_text='Particle box size for picking.',
    )

    motioncor_binning = forms.FloatField(
        label='MotionCor binning',
        initial=1.0,
        help_text='Fourier binning for motion-corrected output (1.0 = no binning).',
    )

    motioncor_patches = forms.IntegerField(
        label='MotionCor patches',
        initial=5,
        help_text='Number of patches for local motion correction (5 = 5x5).',
    )

    picking_threshold = forms.FloatField(
        label='Picking threshold',
        initial=0.3,
        help_text='Confidence threshold for particle picking (0-1).',
    )

    picking_model = forms.CharField(
        label='Picking model path',
        help_text='Path to crYOLO model on HPC (supports glob). Empty = use general model.',
    )

    extract_box_size = forms.IntegerField(
        label='Extraction box size (px)',
        initial=256,
        help_text='Box size for extracted particles.',
    )

    extract_downscale = forms.IntegerField(
        label='Extraction downscale',
        initial=1,
        min_value=1,
        help_text='Downscale extracted particles (1 = no downscale).',
    )

    # ===== Auth =====

    globus_client_id = forms.CharField(
        label='Globus Client ID',
        initial='7df9d534-fb19-4d79-8e83-642f1cdcf081',
        help_text='Globus app client ID (pre-filled with SmartScope app).',
    )

    token_file = forms.CharField(
        label='Token File',
        initial='/opt/config/smartscope_tokens.json',
        help_text='Path to cached Globus auth tokens.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Populate dynamic dropdown choices from Globus APIs
        globus_choices = _get_globus_choices()
        for field_name, field_choices in globus_choices.items():
            if field_name in self.fields and field_choices:
                self.fields[field_name].choices = [('', '— Select —')] + field_choices

        for visible in self.visible_fields():
            widget = visible.field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = 'form-check-input'
            elif not isinstance(widget, forms.RadioSelect):
                widget.attrs['class'] = 'form-control'
            visible.field.required = False
