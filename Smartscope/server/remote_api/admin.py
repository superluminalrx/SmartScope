import re

from django import forms
from django.conf import settings
from django.contrib import admin

from . import devices
from .models import RemoteDevice


class RemoteDeviceForm(forms.ModelForm):

    class Meta:
        model = RemoteDevice
        fields = ('name', 'endpoint_id')

    def clean_endpoint_id(self):
        value = self.cleaned_data['endpoint_id'].strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', value):
            raise forms.ValidationError('A device key is 64 hexadecimal characters.')
        return value


@admin.register(RemoteDevice)
class RemoteDeviceAdmin(admin.ModelAdmin):
    """Pairing is an exchange of keys: the key of the device is entered here, and
    the key of SmartScope shown here is entered in the device."""
    form = RemoteDeviceForm
    list_display = ('name', 'endpoint_id', 'added_by', 'created', 'last_seen')
    search_fields = ('name', 'endpoint_id')
    fields = ('name', 'endpoint_id', 'smartscope_key', 'smartscope_ticket', 'added_by', 'created', 'last_seen')
    readonly_fields = ('smartscope_key', 'smartscope_ticket', 'added_by', 'created', 'last_seen')

    def listener_value(self, key):
        if not settings.REMOTE_API_ENABLED:
            return 'The remote API is disabled. Set REMOTE_API_ENABLED=true to use it.'
        state = devices.listener_state()
        return state[key] if state else 'The remote API listener is not running.'

    @admin.display(description='SmartScope key')
    def smartscope_key(self, obj):
        return self.listener_value('endpoint_id')

    @admin.display(description='SmartScope ticket')
    def smartscope_ticket(self, obj):
        return self.listener_value('ticket')

    def get_fields(self, request, obj=None):
        return self.fields if obj is not None else self.fields[:4]

    def get_readonly_fields(self, request, obj=None):
        # The key is the device. To replace it, delete the entry and add the new key.
        return self.readonly_fields + (('endpoint_id',) if obj is not None else ())

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        super().save_model(request, obj, form, change)
