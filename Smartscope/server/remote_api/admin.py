from django.contrib import admin

from .models import RemoteDevice


@admin.register(RemoteDevice)
class RemoteDeviceAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'short_id', 'created', 'last_seen')
    list_filter = ('user',)
    search_fields = ('name', 'endpoint_id', 'user__username')
    readonly_fields = ('endpoint_id', 'created', 'last_seen')
