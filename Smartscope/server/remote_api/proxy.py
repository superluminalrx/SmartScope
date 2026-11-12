"""Checking and rewriting of the HTTP request a device sends before it is forwarded."""
import re
from typing import Callable, Optional, Tuple

MAX_HEAD = 65536
END_OF_HEAD = b'\r\n\r\n'
REQUEST_LINE = re.compile(r'^[A-Z]+ \S+ HTTP/1\.[01]$')
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# Set by the listener, so whatever the device sent is discarded.
REPLACED = {'connection', 'proxy-connection', 'keep-alive'}

BAD_REQUEST = b'HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'


class BadRequest(Exception):
    pass


def split_head(buffer: bytes) -> Optional[Tuple[bytes, bytes]]:
    """Splits what was received into the request head and the start of the body.

    Returns None while the head is incomplete.
    """
    end = buffer.find(END_OF_HEAD)
    if end == -1:
        if len(buffer) > MAX_HEAD:
            raise BadRequest('Request head too large')
        return None
    if end > MAX_HEAD:
        raise BadRequest('Request head too large')
    return buffer[:end], buffer[end + len(END_OF_HEAD):]


def rewrite_head(head: bytes, host_allowed: Callable[[str], bool], fallback_host: str) -> Tuple[bytes, bool]:
    """Returns the head to forward and whether it asks for a protocol upgrade.

    Credentials are left as the device sent them. The request is made to close
    the upstream connection when it completes, and to name a host the web app
    accepts, which a device has no way to know.
    """
    text = head.decode('latin-1')
    lines = text.split('\r\n')
    # The web server could read a stray CR or LF as a line break and see headers this check did not.
    if any('\r' in line or '\n' in line or '\0' in line for line in lines):
        raise BadRequest('Malformed request head')
    if not REQUEST_LINE.match(lines[0]):
        raise BadRequest('Malformed request line')
    headers = []
    host = None
    upgrade = False
    for line in lines[1:]:
        name, separator, value = line.partition(':')
        if not separator or not HEADER_NAME.match(name):
            raise BadRequest('Malformed header')
        lower = name.lower()
        if lower in REPLACED:
            continue
        if lower == 'host':
            if host is not None:
                raise BadRequest('Repeated Host header')
            host = value.strip()
            continue
        if lower == 'upgrade':
            upgrade = True
        headers.append(line)
    if not host or not host_allowed(host):
        host = fallback_host
    headers = [f'Host: {host}', 'Connection: Upgrade' if upgrade else 'Connection: close'] + headers
    return '\r\n'.join([lines[0]] + headers).encode('latin-1') + END_OF_HEAD, upgrade
