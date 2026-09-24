"""Child process of ``test_database.ProcessTeardownTest`` (not a test itself).

A readiness check times out against a stalled server, the database is disposed
and the event loop is closed. Prints ``done`` when ``asyncio.run`` returned.

    python -m tests.teardown_child cancel_safe | libpq_fallback

The ``audit_`` modes start the whole application against the stalled server
(its startup audit diagnostic is then stuck in its catalog query) and leave
the lifespan, printing ``shutdown <seconds>`` before ``done``. The ``janitor_``
modes do the same with the Research Scratch janitor (PAW-050), whose first purge
is started at once, so that it is inside its purge query when the lifespan ends.
"""

import asyncio
import sys
import threading
import time

import psycopg

from paw_backend.app import create_app
from paw_backend.db import Database
from paw_backend.research.scratch import janitor

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


async def audit_main(port: int) -> None:
    app = create_app(
        make_settings(
            database_url=f"postgresql://paw:pw@127.0.0.1:{port}/paw",
            # Long, so that the diagnostic's own timeout cannot end it first.
            database_timeout_seconds=30,
            shutdown_timeout_seconds=2,
        )
    )
    async with app.router.lifespan_context(app):
        await asyncio.sleep(1.0)  # the diagnostic is now inside its query
        started = time.monotonic()
    print(f"shutdown {time.monotonic() - started:.2f}")


async def janitor_main(port: int) -> None:
    janitor.FIRST_TICK_DELAY_SECONDS = 0.0  # purge at once, not after 30 seconds
    app = create_app(
        make_settings(
            database_url=f"postgresql://paw:pw@127.0.0.1:{port}/paw",
            database_timeout_seconds=30,
            shutdown_timeout_seconds=2,
        )
    )
    async with app.router.lifespan_context(app):
        await asyncio.sleep(1.0)  # the janitor is now inside its purge query
        started = time.monotonic()
    print(f"shutdown {time.monotonic() - started:.2f}")


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode.endswith("libpq_fallback"):
        # What psycopg does with a libpq older than 17.
        psycopg.capabilities.has_cancel_safe = lambda: False
    port = start_stalled_server()
    entry = {"audit_": audit_main, "janitor_": janitor_main}
    asyncio.run(next((f for k, f in entry.items() if mode.startswith(k)), main)(port))
    print("done")
