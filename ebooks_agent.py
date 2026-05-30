"""CLI entry point — start the agent and accept search terms interactively."""

import asyncio
import logging

from agent import EbooksAgent
from unix_socket import unix_socket


async def _input_loop(agent: EbooksAgent):
    while True:
        try:
            term = (await asyncio.to_thread(input, "search> ")).strip()
        except EOFError:
            break
        if not term:
            continue
        if term.lower() in ("exit", "quit"):
            break
        try:
            path = await agent.search(term)
            print(path)
        except TimeoutError as e:
            print(f"Error: {e}")


async def _main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    agent = EbooksAgent()
    await agent.start()

    socket_task = asyncio.create_task(unix_socket(agent))
    try:
        await _input_loop(agent)
    finally:
        socket_task.cancel()
        agent.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
