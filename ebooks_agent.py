"""CLI entry point — start the agent and accept search terms interactively."""

import asyncio
import logging

from agent import EbooksAgent
from unix_socket import unix_socket


async def _input_loop(agent: EbooksAgent):
    # Continuously listen for inputs from an interactive shell
    while True:
        try:
            # Create a new thread for the blocking input function
            # Grab the search term from the shell
            term = (await asyncio.to_thread(input, "search> ")).strip()
        except EOFError:
            break
        if not term:
            continue
        if term.lower() in ("exit", "quit"):
            break
        try:
            # Once we get a search term
            # Pass it to the agent
            # and await a response
            path = await agent.search(term)
            print(path)
        except TimeoutError as e:
            print(f"Error: {e}")


async def _main():
    # Python's build in logging library
    # Minimum log level is INFO (DEBUG messages are suppress)
    # Format defines a message template.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Instantiate our ebooks agent
    agent = EbooksAgent()
    # Wait for the agent to start and connect to the server
    # We don't want to handle any input until it's ready to receive
    await agent.start()

    # Spawn a new task to handle the unix socket
    # Pass it a reference to the agent so we can feed
    # it input from the socket
    socket_task = asyncio.create_task(unix_socket(agent))
    try:
        # Start our input loop
        # The unix socket will run on a different task
        # while this loop continually listens for input
        # from the CLI
        await _input_loop(agent)
    finally:
        # When we exit out, we close our socket and end the task
        # and disconnect agent from IRC server
        socket_task.cancel()
        agent.stop()

# __name__ captures the module (entrypoint?) name
# Detects whether this module was run as such
# rather than imported into another. 
if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
