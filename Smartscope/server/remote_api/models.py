from django.conf import settings
from django.db import models


class RemoteDevice(models.Model):
    """A device that may reach the web app through the remote listener as its owner."""
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='remote_devices')
    name = models.CharField(max_length=100)
    endpoint_id = models.CharField(max_length=64, unique=True, help_text='Public key of the device, as hexadecimal.')
    created = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['user__username', 'name']

    def __str__(self):
        return f'{self.name} ({self.user})'

    @property
    def short_id(self):
        return self.endpoint_id[:10]
