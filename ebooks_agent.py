"""CLI entry point — start the agent and accept search terms interactively."""

import asyncio
import logging

from agent import EbooksAgent

async def _main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    agent = EbooksAgent()
    await agent.start()

    try:
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
    finally:
        agent.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
