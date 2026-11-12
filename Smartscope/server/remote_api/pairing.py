"""Pairing codes, device registration and the listener's published state."""
import hashlib
import secrets
import time
from datetime import timedelta
from typing import Optional

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone
from rest_framework.authtoken.models import Token

from .models import RemoteDevice
from .protocol import HTTP_ALPN, PAIR_ALPN, pairing_link  # noqa: F401

PAIRING_SECONDS = 600
STATE_KEY = 'remote_api_state'
STATE_SECONDS = 90
LAST_SEEN_INTERVAL = timedelta(minutes=1)


def _code_key(code: str) -> str:
    return 'remote_api_pair_' + hashlib.sha256(code.encode()).hexdigest()


def create_pairing(user) -> str:
    code = secrets.token_urlsafe(24)
    cache.set(_code_key(code), user.pk, PAIRING_SECONDS)
    return code


def claim_pairing(code: str) -> Optional[int]:
    """Returns the id of the user who created the code and invalidates it."""
    key = _code_key(code)
    user_id = cache.get(key)
    # delete() reports whether this caller removed the key, so a code cannot be claimed twice.
    if user_id is None or not cache.delete(key):
        return None
    return user_id


def pair_device(endpoint_id: str, code: str, name: str) -> dict:
    """Registers the device for the user who created the code. Returns the reply sent to the device."""
    close_old_connections()
    user_id = claim_pairing(code) if isinstance(code, str) else None
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first() if user_id is not None else None
    if user is None:
        return {'ok': False, 'error': 'The pairing code is not valid or has expired.'}
    name = (name if isinstance(name, str) else '').strip()[:100] or 'Device'
    device = RemoteDevice.objects.filter(endpoint_id=endpoint_id).first()
    if device is not None and device.user_id != user.pk:
        return {'ok': False, 'error': 'This device is already paired with another account.'}
    if device is None:
        device = RemoteDevice(user=user, endpoint_id=endpoint_id)
    device.name = name
    device.save()
    return {'ok': True, 'user': user.get_username(), 'device': device.name}


def authorize_device(endpoint_id: str) -> Optional[str]:
    """Returns the API token of the device's owner, or None when the device may not connect."""
    close_old_connections()
    device = RemoteDevice.objects.select_related('user').filter(endpoint_id=endpoint_id).first()
    if device is None or not device.user.is_active:
        return None
    now = timezone.now()
    if device.last_seen is None or now - device.last_seen > LAST_SEEN_INTERVAL:
        RemoteDevice.objects.filter(pk=device.pk).update(last_seen=now)
    token, _ = Token.objects.get_or_create(user=device.user)
    return token.key


def publish_state(endpoint_id: str, ticket: str) -> None:
    cache.set(STATE_KEY, {'endpoint_id': endpoint_id, 'ticket': ticket, 'time': time.time()}, STATE_SECONDS)


def listener_state() -> Optional[dict]:
    """State published by a running listener, None when no listener is running."""
    return cache.get(STATE_KEY)
