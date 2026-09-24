"""Child process of ``test_database.ProcessTeardownTest`` (not a test itself).

A readiness check times out against a stalled server, the database is disposed
and the event loop is closed. Prints ``done`` when ``asyncio.run`` returned.

    python -m tests.teardown_child cancel_safe | libpq_fallback
"""

import asyncio
import sys
import threading

import psycopg

from paw_backend.db import Database

from .fake_postgres import HangingPostgres
from .support import make_settings


def start_stalled_server() -> int:
    """Run the fake server on its own thread; it keeps running until exit."""
    ready = threading.Event()
    ports: list[int] = []

    def serve() -> None:
        async def run() -> None:
            async with HangingPostgres() as server:
                ports.append(server.port)
                ready.set()
                await asyncio.Event().wait()

        asyncio.run(run())

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    return ports[0]


async def main(port: int) -> None:
    database = Database(
        make_settings(
            database_url=f"postgresql://paw:pw@127.0.0.1:{port}/paw",
            database_timeout_seconds=0.3,
            shutdown_timeout_seconds=2,
        )
    )
    await database.check()  # times out; the probe is still being stopped
    await database.dispose()


if __name__ == "__main__":
    if sys.argv[1] == "libpq_fallback":
        # What psycopg does with a libpq older than 17.
        psycopg.capabilities.has_cancel_safe = lambda: False
    asyncio.run(main(start_stalled_server()))
    print("done")
