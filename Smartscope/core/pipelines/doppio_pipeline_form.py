
from django import forms


# Fields that should only show when mode = "transfer_and_process"
PROCESS_ONLY_FIELDS = {
    'grouping', 'globus_compute_endpoint_id', 'globus_flow_id',
    'do_motioncor', 'do_ctf', 'do_miffi', 'do_picking', 'do_extraction',
    'pixel_size_override', 'dose_per_frame', 'box_size',
    'motioncor_binning', 'motioncor_patches',
    'picking_threshold', 'picking_model',
    'extract_box_size', 'extract_downscale',
}


class DoppioPipelineForm(forms.Form):
    mode = forms.ChoiceField(
        choices=[
            ('transfer_and_process', 'Transfer and Process (Globus Transfer + Doppio)'),
            ('transfer_only', 'Transfer Only (Globus Transfer to HPC)'),
        ],
        widget=forms.Select(attrs={
            'onchange': (
                'var p=this.value==="transfer_and_process";'
                'document.querySelectorAll("[data-process-only]").forEach(function(el){'
                'var w=el.closest(".my-1")||el.closest(".input-group");'
                'if(w){w.style.opacity=p?"1":"0.4";'
                'w.style.pointerEvents=p?"auto":"none";}'
                '})'
            ),
        }),
        help_text='Transfer only moves frames to HPC. Transfer and process also runs Doppio via Globus Compute.'
    )

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

    # ===== Globus =====

    globus_compute_endpoint_id = forms.CharField(
        label='Globus Compute Endpoint',
        help_text='UUID of the Globus Compute endpoint where Doppio will run.',
    )

    globus_flow_id = forms.CharField(
        label='Globus Flow ID',
        initial='ddffd8b0-81dd-4325-9383-ea16f411eaa8',
        help_text='UUID of the deployed Globus Flow (transfer -> compute -> transfer).',
    )

    source_collection_id = forms.CharField(
        label='Source Collection',
        help_text='Globus collection on the microscope side (e.g. MSU Talos Arctica).',
    )

    destination_collection_id = forms.CharField(
        label='Destination Collection',
        help_text='Globus collection on the HPC side (e.g. Blackmore - Superluminal).',
    )

    source_base_path = forms.CharField(
        label='Source Base Path',
        help_text='Globus path prefix mapping to the container data root. '
                  'e.g. "/SmartScope" if that maps to /mnt/data inside the container.',
    )

    destination_base_path = forms.CharField(
        label='Destination Base Path',
        help_text='Root on HPC where Doppio projects live, e.g. "/data/programs".',
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
    # pixel_size, voltage, Cs, gain_rot, gain_flip are auto-derived from microscope metadata

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
        for visible in self.visible_fields():
            widget = visible.field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = 'form-check-input'
            elif not isinstance(widget, forms.RadioSelect):
                widget.attrs['class'] = 'form-control'
            visible.field.required = False

            # Tag processing-only fields so JS can toggle them
            if visible.name in PROCESS_ONLY_FIELDS:
                widget.attrs['data-process-only'] = 'true'

