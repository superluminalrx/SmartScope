from django.conf import settings
from django.db import models


class RemoteDevice(models.Model):
    """A device whose key lets it reach the web app through the remote listener.

    The key only opens the connection. Requests through it authenticate as a
    user the same way they do on the local network.
    """
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=100)
    endpoint_id = models.CharField('device key', max_length=64, unique=True,
                                   help_text='Public key of the device: 64 hexadecimal characters, shown by the device.')
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='+')
    created = models.DateTimeField('added', auto_now_add=True)
    last_seen = models.DateTimeField('last connected', null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name
