"""The listener process: accepts paired devices over iroh and forwards their requests."""
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http.request import split_domain_port, validate_host

from . import pairing, proxy

logger = logging.getLogger(__name__)

HEAD_TIMEOUT = 30
PAIR_TIMEOUT = 30
ONLINE_TIMEOUT = 15
TOKEN_SECONDS = 10
PUBLISH_INTERVAL = 30
MAX_STREAMS = 64
CHUNK = 65536

BAD_GATEWAY = b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'


def load_secret_key(path: Path) -> bytes:
    """The key is the address devices were given, so it is kept across restarts."""
    import iroh
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    key = iroh.SecretKey.generate().to_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
        file.write(key.hex())
    return key


def endpoint_options(secret_key: bytes, relays: str, bind_addr: str):
    import iroh
    options = dict(secret_key=secret_key, alpns=[pairing.PAIR_ALPN, pairing.HTTP_ALPN])
    urls = [url.strip() for url in relays.split(',') if url.strip()]
    if relays.strip().lower() == 'disabled':
        options.update(preset=iroh.preset_minimal(), relay_mode=iroh.RelayMode.disabled())
    elif urls:
        options.update(preset=iroh.preset_minimal(), relay_mode=iroh.RelayMode.custom_from_urls(urls))
    else:
        options.update(preset=iroh.preset_n0())
    if bind_addr:
        options.update(bind_addr=bind_addr)
    return iroh.EndpointOptions(**options)


def host_allowed(host: str) -> bool:
    domain, _ = split_domain_port(host)
    return bool(domain) and validate_host(domain, settings.ALLOWED_HOSTS)


def fallback_host() -> str:
    return next((host for host in settings.ALLOWED_HOSTS if host and host[0] not in '.*'), 'localhost')


async def read_head(recv) -> Tuple[bytes, bytes]:
    buffer = b''
    while True:
        parts = proxy.split_head(buffer)
        if parts is not None:
            return parts
        data = await recv.read(CHUNK)
        if not data:
            raise proxy.BadRequest('Incomplete request head')
        buffer += data


async def pump_to_upstream(recv, writer) -> None:
    import iroh
    try:
        while True:
            data = await recv.read(CHUNK)
            if not data:
                # The end of the request is not passed on: nginx drops a proxied
                # request when its client half-closes the connection.
                return
            writer.write(data)
            await writer.drain()
    except (iroh.IrohError, OSError):
        return


async def pump_to_device(reader, send) -> None:
    while True:
        data = await reader.read(CHUNK)
        if not data:
            return
        await send.write_all(data)


class Listener:

    def __init__(self, upstream: Tuple[str, int], key_file: Path, relays: str = '', bind_addr: str = ''):
        self.upstream = upstream
        self.key_file = key_file
        self.relays = relays
        self.bind_addr = bind_addr
        self.endpoint = None
        self.tasks = set()
        self.tokens = {}

    def spawn(self, coroutine) -> None:
        # The loop only keeps weak references to tasks.
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.finished)

    def finished(self, task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error('Remote API task failed', exc_info=task.exception())

    async def start(self) -> None:
        import iroh
        options = endpoint_options(load_secret_key(self.key_file), self.relays, self.bind_addr)
        self.endpoint = await iroh.Endpoint.bind(options)
        logger.info(f'Remote API listening as {self.endpoint.id()} on {self.endpoint.bound_sockets()}')

    def ticket(self) -> str:
        import iroh
        return str(iroh.EndpointTicket.from_addr(self.endpoint.addr()))

    async def serve(self) -> None:
        await self.start()
        self.spawn(self.publish())
        await self.accept()

    async def accept(self) -> None:
        while True:
            incoming = await self.endpoint.accept_next()
            if incoming is None:
                return
            self.spawn(self.handle_incoming(incoming))

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await self.endpoint.close()

    async def publish(self) -> None:
        if self.relays.strip().lower() != 'disabled':
            try:
                # Without a relay the ticket only holds addresses of this network.
                await asyncio.wait_for(self.endpoint.online(), ONLINE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning('Remote API could not reach a relay, devices outside this network may not connect.')
        while True:
            await sync_to_async(pairing.publish_state)(str(self.endpoint.id()), self.ticket())
            await asyncio.sleep(PUBLISH_INTERVAL)

    async def token(self, remote: str) -> Optional[str]:
        """API token of the device's owner, looked up again every few seconds so that
        removing a device also ends its open connections."""
        expires, token = self.tokens.get(remote, (0, None))
        if expires > time.monotonic():
            return token
        self.tokens.pop(remote, None)
        token = await sync_to_async(pairing.authorize_device)(remote)
        if token is not None:
            self.tokens[remote] = (time.monotonic() + TOKEN_SECONDS, token)
        return token

    async def handle_incoming(self, incoming) -> None:
        import iroh
        remote = 'unknown'
        try:
            connection = await (await incoming.accept()).connect()
            remote = str(connection.remote_id())
            alpn = bytes(connection.alpn())
            if alpn == pairing.PAIR_ALPN:
                await self.handle_pairing(connection, remote)
            elif alpn == pairing.HTTP_ALPN:
                await self.handle_device(connection, remote)
            else:
                connection.close(400, b'unknown protocol')
        except iroh.IrohError as err:
            logger.debug(f'Connection from {remote} ended: {err}')

    async def handle_pairing(self, connection, remote: str) -> None:
        try:
            stream = await asyncio.wait_for(connection.accept_bi(), PAIR_TIMEOUT)
            raw = await asyncio.wait_for(stream.recv().read_to_end(4096), PAIR_TIMEOUT)
        except asyncio.TimeoutError:
            connection.close(408, b'timeout')
            return
        try:
            request = json.loads(raw)
            code, name = request.get('code'), request.get('name')
        except (ValueError, AttributeError):
            code, name = None, None
        reply = await sync_to_async(pairing.pair_device)(remote, code, name)
        logger.info(f"Pairing of device {remote}: {'user ' + reply['user'] if reply['ok'] else reply['error']}")
        send = stream.send()
        await send.write_all(json.dumps(reply).encode())
        await send.finish()
        try:
            # Closing first could discard the reply before it is delivered.
            await asyncio.wait_for(connection.closed(), 10)
        except asyncio.TimeoutError:
            connection.close(0, b'')

    async def handle_device(self, connection, remote: str) -> None:
        if await self.token(remote) is None:
            logger.warning(f'Refused device {remote}, it is not paired.')
            connection.close(403, b'device not paired')
            return
        connection.set_max_concurrent_bi_streams(MAX_STREAMS)
        logger.info(f'Device {remote} connected.')
        while True:
            stream = await connection.accept_bi()
            self.spawn(self.handle_stream(connection, stream, remote))

    async def handle_stream(self, connection, stream, remote: str) -> None:
        import iroh
        recv, send = stream.recv(), stream.send()
        writer = None
        try:
            token = await self.token(remote)
            if token is None:
                connection.close(403, b'device not paired')
                return
            try:
                head, body = await asyncio.wait_for(read_head(recv), HEAD_TIMEOUT)
                head, upgrade = proxy.rewrite_head(head, token, host_allowed, fallback_host())
            except (proxy.BadRequest, asyncio.TimeoutError):
                await send.write_all(proxy.BAD_REQUEST)
                await send.finish()
                return
            try:
                reader, writer = await asyncio.open_connection(*self.upstream)
            except OSError as err:
                logger.error(f'Remote API could not reach the web app at {self.upstream}: {err}')
                await send.write_all(BAD_GATEWAY)
                await send.finish()
                return
            writer.write(head + body)
            await writer.drain()
            upload = asyncio.ensure_future(pump_to_upstream(recv, writer))
            download = asyncio.ensure_future(pump_to_device(reader, send))
            try:
                # An upgraded connection ends when either side does, a request when it is answered.
                await asyncio.wait([upload, download] if upgrade else [download],
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                upload.cancel()
                download.cancel()
                results = await asyncio.gather(upload, download, return_exceptions=True)
            if isinstance(results[1], (iroh.IrohError, OSError)):
                raise results[1]
            await send.finish()
        except (iroh.IrohError, OSError) as err:
            logger.debug(f'Request from device {remote} ended early: {err}')
        finally:
            if writer is not None:
                writer.close()


def remote_api() -> None:
    """Runs the listener. Called as `smartscope.py remote_api`."""
    # As the main process of a container, Python would otherwise ignore the stop signal.
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
    if not settings.REMOTE_API_ENABLED:
        logger.info('Remote API is disabled. Set REMOTE_API_ENABLED=true to enable it.')
        threading.Event().wait()
        return
    host, _, port = settings.REMOTE_API_UPSTREAM.rpartition(':')
    listener = Listener((host, int(port)), Path(settings.REMOTE_API_KEY_FILE),
                        settings.REMOTE_API_RELAYS, settings.REMOTE_API_BIND)
    asyncio.run(listener.serve())
