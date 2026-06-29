"""
NiimBot B1 — usage examples

Three patterns are shown:

1. Simple one-shot print (original context-manager style).
2. Persistent session: connect once, print many jobs, then disconnect.
3. Subprocess-isolated worker: the safe way to drive the printer from a
   gevent/eventlet server such as Flask-SocketIO.
"""

import asyncio
from niimbot_b1 import (
    NiimbotB1,
    PrinterSession,
    PrinterWorker,
    discover_printer,
)


# ---------------------------------------------------------------------------
# 1. Simple one-shot print (connects and disconnects around a single job)
# ---------------------------------------------------------------------------
async def simple_print():
    address = await discover_printer()
    if not address:
        print("No printer found")
        return

    async with NiimbotB1(address) as printer:
        print(f"Battery: {await printer.get_battery()}%")
        await printer.print_image("image.png")


# ---------------------------------------------------------------------------
# 2. Persistent session: stay connected across multiple jobs
# ---------------------------------------------------------------------------
async def persistent_session():
    # Handles Linux adapter readiness, address caching, rediscovery and
    # reconnect automatically. Connect once...
    session = PrinterSession(adapter="hci0")
    await session.connect()
    try:
        print(f"Battery: {await session.get_battery()}%")

        # ...print as many jobs as you like while staying connected.
        await session.print_image("label1.png")
        await session.print_image("label2.png")
        await session.print_image("label3.png", copies=2)
    finally:
        # Disconnect manually (or just power off the printer).
        await session.disconnect()


# ---------------------------------------------------------------------------
# 3. Subprocess-isolated worker (use this inside Flask-SocketIO / gevent apps)
# ---------------------------------------------------------------------------
def worker_example():
    # All BLE work runs in a separate process with a clean asyncio loop, so it
    # is unaffected by gevent/eventlet monkeypatching in the web server.
    worker = PrinterWorker(adapter="hci0")
    worker.start()
    try:
        worker.connect()
        print(f"Battery: {worker.get_battery()}%")

        # Persistent connection — print multiple jobs without reconnecting.
        worker.print_image("label1.png")
        worker.print_image("label2.png")
    finally:
        worker.shutdown()


# ---------------------------------------------------------------------------
# Flask-SocketIO sketch — create one worker for the app's lifetime
# ---------------------------------------------------------------------------
#
#   printer = PrinterWorker(adapter="hci0")
#   printer.start()
#   printer.connect()          # connect once at startup
#
#   @socketio.on("print")
#   def handle_print(data):
#       printer.print_image(data["path"])   # reuses the live connection
#
#   import atexit
#   atexit.register(printer.shutdown)        # disconnect on app exit


if __name__ == "__main__":
    # Pick one of the examples to run:
    asyncio.run(simple_print())
    # asyncio.run(persistent_session())
    # worker_example()