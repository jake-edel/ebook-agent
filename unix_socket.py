"""
Unix socket server — accepts search terms, returns file paths.

Test: echo "dune frank herbert" | socat - UNIX-CONNECT:/tmp/ebooks.sock
"""

import asyncio
import os

SOCKET_PATH = "/tmp/ebooks.sock"


# Factory function that closes over agent, making it available to handle()
# without changing its signature — asyncio expects (reader, writer) only
def _make_handler(agent):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # Read line by line, decode bytes to text
        data = await reader.readline()
        term = data.decode().strip()

        # Nothing there — close up shop
        if not term:
            writer.close()
            return

        try:
            # Pass the search term to our agent 
            # Write the response back to the external source
            path = await agent.search(term)
            writer.write(f"{path}\n".encode())
        except TimeoutError as e:
            writer.write(f"error: {e}\n".encode())

        try:
            # Flush once we're done — caller has probably already closed their end by now
            await writer.drain()
        except ConnectionResetError:
            # client (e.g. socat) closed its end before drain finished
            # response was already written
            pass

        writer.close()

    return handle


async def unix_socket(agent):
    # Clean up any existing socket file, swallow the error when one doesn't exist
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    # Pass asyncio our handler, which tells the socket server what to do when bytes arrive
    # asyncio because who knows when those bytes are showing up
    server = await asyncio.start_unix_server(_make_handler(agent), path=SOCKET_PATH)
    print(f"Listening on {SOCKET_PATH}")
    async with server:
        await server.serve_forever()
