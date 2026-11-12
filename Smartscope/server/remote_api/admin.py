from django.contrib import admin

from .models import RemoteDevice


@admin.register(RemoteDevice)
class RemoteDeviceAdmin(admin.ModelAdmin):
    list_display = ('name', 'short_id', 'paired_by', 'created', 'last_seen')
    search_fields = ('name', 'endpoint_id')
    readonly_fields = ('endpoint_id', 'paired_by', 'created', 'last_seen')

    def has_add_permission(self, request):
        # A device registers its own key when it pairs, so one typed in here could never connect.
        return False
