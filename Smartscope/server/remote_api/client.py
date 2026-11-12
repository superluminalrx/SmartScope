"""Reference client for the remote API.

Opens a local port that forwards to SmartScope, so any HTTP client can use the
web app and API through it with its usual credentials:

    python -m Smartscope.server.remote_api.client key
    python -m Smartscope.server.remote_api.client forward <SmartScope key or ticket> 48010
    curl -H 'Authorization: Token <your API token>' http://localhost:48010/api/

`key` prints the key of this device, to be registered in the SmartScope admin site.
"""
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Optional

import iroh

from .protocol import HTTP_ALPN

CHUNK = 65536
CONFIG = Path.home() / '.smartscope_remote'


async def bind(key_file: Path, relays: bool = True):
    """Opens the endpoint of this device. Its key is what SmartScope registers."""
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
            file.write(iroh.SecretKey.generate().to_bytes().hex())
    key = bytes.fromhex(key_file.read_text().strip())
    if relays:
        return await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=key))
    return await iroh.Endpoint.bind(iroh.EndpointOptions(
        preset=iroh.preset_minimal(), secret_key=key, relay_mode=iroh.RelayMode.disabled()))


def address(target: str):
    """Address of SmartScope from its key, found through discovery, or from its ticket."""
    target = target.strip()
    if re.fullmatch(r'[0-9a-fA-F]{64}', target):
        return iroh.EndpointAddr(iroh.EndpointId.from_string(target), None, [])
    return iroh.EndpointTicket.from_string(target).endpoint_addr()


class Forwarder:
    """Local TCP port whose connections are each carried by one stream to SmartScope."""

    def __init__(self, endpoint, target: str):
        self.endpoint = endpoint
        self.target = target
        self.connection = None
        self.server: Optional[asyncio.AbstractServer] = None

    async def connect(self):
        if self.connection is None or self.connection.close_reason() is not None:
            self.connection = await self.endpoint.connect(address(self.target), HTTP_ALPN)
        return self.connection

    async def start(self, host: str = '127.0.0.1', port: int = 0) -> int:
        self.server = await asyncio.start_server(self.handle, host, port)
        return self.server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer) -> None:
        try:
            stream = await (await self.connect()).open_bi()
            send, recv = stream.send(), stream.recv()

            async def upload():
                while True:
                    data = await reader.read(CHUNK)
                    if not data:
                        return await send.finish()
                    await send.write_all(data)

            async def download():
                while True:
                    data = await recv.read(CHUNK)
                    if not data:
                        return
                    writer.write(data)
                    await writer.drain()

            task = asyncio.ensure_future(upload())
            try:
                await download()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        except (iroh.IrohError, OSError):
            pass
        finally:
            writer.close()

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


async def main(args) -> None:
    endpoint = await bind(CONFIG / 'key')
    if args[0] == 'key':
        print(endpoint.id())
    elif args[0] == 'forward':
        forwarder = Forwarder(endpoint, args[1])
        port = await forwarder.start(port=int(args[2]) if len(args) > 2 else 48010)
        print(f'Forwarding http://localhost:{port} to SmartScope. Stop with Ctrl-C.')
        await asyncio.Event().wait()
    await endpoint.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1:]))
