
from django import forms


class DoppioPipelineForm(forms.Form):
    mode = forms.ChoiceField(
        choices=[
            ('transfer_only', 'Transfer Only (Globus Transfer to HPC)'),
            ('transfer_and_process', 'Transfer and Process (Globus Transfer + Doppio)'),
        ],
        help_text='Transfer only moves frames to HPC. Transfer and process also runs Doppio via Globus Compute.'
    )

    globus_compute_endpoint_id = forms.CharField(
        label='Globus Compute Endpoint ID',
        help_text='UUID of the Globus Compute endpoint where Doppio will run.'
    )

    source_collection_id = forms.CharField(
        label='Source Collection ID',
        help_text='Globus collection ID on the microscope/SmartScope side.'
    )

    destination_collection_id = forms.CharField(
        label='Destination Collection ID',
        help_text='Globus collection ID on the HPC side.'
    )

    source_base_path = forms.CharField(
        label='Source Base Path',
        help_text='Base path on the source collection (e.g. /mnt/data/).'
    )

    destination_base_path = forms.CharField(
        label='Destination Base Path',
        help_text='Base path on the HPC collection (e.g. /scratch/user/smartscope/).'
    )

    frames_directory = forms.CharField(
        label='Frames Directory',
        help_text='Path to frame data on the source side.'
    )

    globus_client_id = forms.CharField(
        label='Globus Client ID',
        help_text='Globus confidential app client ID.'
    )

    globus_client_secret_file = forms.CharField(
        label='Globus Client Secret File',
        help_text='Path to file containing the Globus client secret.'
    )

    poll_interval = forms.FloatField(
        initial=5.0,
        min_value=1.0,
        help_text='Seconds between polling for job completion.'
    )

    batch_size = forms.IntegerField(
        initial=10,
        min_value=1,
        help_text='Number of images to batch per Doppio job submission.'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for visible in self.visible_fields():
            visible.field.widget.attrs['class'] = 'form-control'
            visible.field.required = False
