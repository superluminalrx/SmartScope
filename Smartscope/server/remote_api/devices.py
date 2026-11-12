"""Lookup of registered devices and the listener's published state."""
import time
from datetime import timedelta
from typing import Optional

from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

from .models import RemoteDevice

STATE_KEY = 'remote_api_state'
STATE_SECONDS = 90
LAST_SEEN_INTERVAL = timedelta(minutes=1)


def authorize_device(endpoint_id: str) -> bool:
    """Whether the key is registered. Records that the device connected."""
    close_old_connections()
    device = RemoteDevice.objects.filter(endpoint_id=endpoint_id).first()
    if device is None:
        return False
    now = timezone.now()
    if device.last_seen is None or now - device.last_seen > LAST_SEEN_INTERVAL:
        RemoteDevice.objects.filter(pk=device.pk).update(last_seen=now)
    return True


def publish_state(endpoint_id: str, ticket: str) -> None:
    cache.set(STATE_KEY, {'endpoint_id': endpoint_id, 'ticket': ticket, 'time': time.time()}, STATE_SECONDS)


def listener_state() -> Optional[dict]:
    """State published by a running listener, None when no listener is running."""
    return cache.get(STATE_KEY)
