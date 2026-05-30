"""
Unix socket server — accepts search terms, returns file paths.

Test: echo "dune frank herbert" | socat - UNIX-CONNECT:/tmp/ebooks.sock
"""

import asyncio
import os

SOCKET_PATH = "/tmp/ebooks.sock"


def _make_handler(agent):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.readline()
        term = data.decode().strip()
        if not term:
            writer.close()
            return
        try:
            path = await agent.search(term)
            writer.write(f"{path}\n".encode())
        except TimeoutError as e:
            writer.write(f"error: {e}\n".encode())
        await writer.drain()
        writer.close()
    return handle


async def unix_socket(agent):
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(_make_handler(agent), path=SOCKET_PATH)
    print(f"Listening on {SOCKET_PATH}")
    async with server:
        await server.serve_forever()
