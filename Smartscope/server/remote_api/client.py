"""Reference client for the remote API.

Pairs this machine with an account and opens a local port that forwards to
SmartScope, so any HTTP client can use the API through it:

    python -m Smartscope.server.remote_api.client pair '<pairing link>' 'My laptop'
    python -m Smartscope.server.remote_api.client forward 48010
    curl http://localhost:48010/api/
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

import iroh

from .protocol import HTTP_ALPN, PAIR_ALPN, parse_pairing_link

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


def address(ticket: str):
    return iroh.EndpointTicket.from_string(ticket).endpoint_addr()


async def pair(endpoint, link: str, name: str) -> dict:
    """Registers this device with the account that created the pairing link."""
    ticket, code = parse_pairing_link(link)
    connection = await endpoint.connect(address(ticket), PAIR_ALPN)
    stream = await connection.open_bi()
    send = stream.send()
    await send.write_all(json.dumps({'code': code, 'name': name}).encode())
    await send.finish()
    reply = json.loads(await stream.recv().read_to_end(4096))
    connection.close(0, b'')
    return reply | {'ticket': ticket}


class Forwarder:
    """Local TCP port whose connections are each carried by one stream to SmartScope."""

    def __init__(self, endpoint, ticket: str):
        self.endpoint = endpoint
        self.ticket = ticket
        self.connection = None
        self.server: Optional[asyncio.AbstractServer] = None

    async def connect(self):
        if self.connection is None or self.connection.close_reason() is not None:
            self.connection = await self.endpoint.connect(address(self.ticket), HTTP_ALPN)
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
    if args[0] == 'pair':
        reply = await pair(endpoint, args[1], args[2] if len(args) > 2 else os.uname().nodename)
        if reply['ok']:
            (CONFIG / 'ticket').write_text(reply['ticket'])
            print(f"Paired as {reply['device']} for user {reply['user']}.")
        else:
            print(reply['error'])
    elif args[0] == 'forward':
        forwarder = Forwarder(endpoint, (CONFIG / 'ticket').read_text().strip())
        port = await forwarder.start(port=int(args[1]) if len(args) > 1 else 48010)
        print(f'Forwarding http://localhost:{port} to SmartScope. Stop with Ctrl-C.')
        await asyncio.Event().wait()
    await endpoint.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1:]))
