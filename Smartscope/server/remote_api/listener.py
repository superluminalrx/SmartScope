"""The listener process: accepts paired devices over iroh and forwards their requests."""
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Tuple

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http.request import split_domain_port, validate_host

from . import devices, proxy
from .protocol import HTTP_ALPN

logger = logging.getLogger(__name__)

HEAD_TIMEOUT = 30
ONLINE_TIMEOUT = 15
RECHECK_SECONDS = 10
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
    options = dict(secret_key=secret_key, alpns=[HTTP_ALPN])
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
        self.checked = {}

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
            await sync_to_async(devices.publish_state)(str(self.endpoint.id()), self.ticket())
            await asyncio.sleep(PUBLISH_INTERVAL)

    async def registered(self, remote: str) -> bool:
        """Looked up again every few seconds so that removing a device also ends its open connections."""
        if self.checked.get(remote, 0) > time.monotonic():
            return True
        self.checked.pop(remote, None)
        if not await sync_to_async(devices.authorize_device)(remote):
            return False
        self.checked[remote] = time.monotonic() + RECHECK_SECONDS
        return True

    async def handle_incoming(self, incoming) -> None:
        import iroh
        remote = 'unknown'
        try:
            connection = await (await incoming.accept()).connect()
            remote = str(connection.remote_id())
            await self.handle_device(connection, remote)
        except iroh.IrohError as err:
            logger.debug(f'Connection from {remote} ended: {err}')

    async def handle_device(self, connection, remote: str) -> None:
        if not await self.registered(remote):
            logger.warning(f'Refused device {remote}, its key is not registered.')
            connection.close(403, b'device not registered')
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
            if not await self.registered(remote):
                connection.close(403, b'device not registered')
                return
            try:
                head, body = await asyncio.wait_for(read_head(recv), HEAD_TIMEOUT)
                head, upgrade = proxy.rewrite_head(head, host_allowed, fallback_host())
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
    """Runs the listener. Called as `smartscope.py remote_api`, by the entrypoint of the web container."""
    if not settings.REMOTE_API_ENABLED:
        logger.info('Remote API is disabled. Set REMOTE_API_ENABLED=true to enable it.')
        return
    host, _, port = settings.REMOTE_API_UPSTREAM.rpartition(':')
    listener = Listener((host, int(port)), Path(settings.REMOTE_API_KEY_FILE),
                        settings.REMOTE_API_RELAYS, settings.REMOTE_API_BIND)
    asyncio.run(listener.serve())
