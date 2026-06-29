# NiimBot B1 Label Printer Library

A Python library for interfacing with NiimBot B1 thermal label printers via Bluetooth LE. Requires only two dependencies: `bleak` for BLE communication and `Pillow` for image processing—no bloated SDKs or proprietary tools needed.

## Installation

You can install this library directly from the source:

```bash
pip install .
```

Or if you want to install it in editable mode for development:

```bash
pip install -e .
```

You can also install it directly from GitHub (if hosted there):

```bash
pip install git+https://github.com/grossrc/NiimbotB1_Simple_Print.git
```

## Configuration Options

```python
NiimbotB1(
    address="XX:XX:XX:XX:XX:XX",  # Printer Bluetooth address; find using the script below
    density=3,                    # Print density (1-5, default 3)
    label_type=LabelType.WITH_GAPS  # Label type (default WITH_GAPS)
)
```

**Label Types:** `WITH_GAPS`, `BLACK`, `CONTINUOUS`, `TRANSPARENT`, `PERFORATED`

| Type | Value | Description |
|------|-------|-------------|
| `WITH_GAPS` | 1 | Standard labels with gaps between them (most common) |
| `BLACK` | 2 | Labels with black marks for positioning |
| `CONTINUOUS` | 3 | Continuous tape without gaps |
| `TRANSPARENT` | 5 | Clear/transparent label stock |
| `PERFORATED` | 4 | Labels with perforated tear lines |

## API

| Method | Description |
|--------|-------------|
| `print_image(path, copies=1)` | Print an image file |
| `get_battery()` | Get battery percentage |
| `get_rfid()` | Get label roll info (dimensions, remaining count) |
| `calibrate()` | Calibrate label sensor (run after changing rolls) |
| `get_print_status()` | Get current print status |

## Persistent Connections & Production Deployment

The basic `async with NiimbotB1(...)` pattern connects and disconnects around a
single job. For real deployments (e.g. a Raspberry Pi print server) you usually
want to **connect once, print many jobs, then disconnect manually or by powering
off**. Two higher-level helpers are provided for this.

### `PrinterSession` — stay connected across jobs

`PrinterSession` keeps a single BLE connection alive and adds the hardening
needed on Linux/BlueZ:

- **Adapter readiness** — runs `rfkill unblock bluetooth` and `bluetoothctl power on` before connecting.
- **`org.bluez.Error.Busy` handling** — retries adapter power-on after a short delay.
- **Address caching** — caches the discovered address on disk for fast reconnects.
- **Cache invalidation** — clears the cached address and forces rediscovery after a connection failure.
- **Reconnect/retry** — automatically reconnects if the link drops between jobs.

```python
from niimbot_b1 import PrinterSession
import asyncio

async def main():
    session = PrinterSession(adapter="hci0")
    await session.connect()              # connect once
    try:
        await session.print_image("label1.png")
        await session.print_image("label2.png")   # reuses the live connection
    finally:
        await session.disconnect()        # or just power off the printer

asyncio.run(main())
```

### `PrinterWorker` — subprocess isolation for gevent / Flask-SocketIO

BLE printing requires a clean `asyncio` event loop. Inside a gevent- or
eventlet-monkeypatched process (such as a Flask-SocketIO server), `asyncio` and
`bleak` can misbehave. `PrinterWorker` runs **all** printer operations in a
separate process (spawned with a fresh, unpatched interpreter) and exposes a
simple synchronous API. The connection stays alive across jobs.

```python
from niimbot_b1 import PrinterWorker

worker = PrinterWorker(adapter="hci0")
worker.start()       # spawn the isolated process
worker.connect()     # connect once
worker.print_image("label1.png")
worker.print_image("label2.png")
worker.shutdown()    # disconnect + stop the process
```

Flask-SocketIO sketch — one worker for the app's lifetime:

```python
from niimbot_b1 import PrinterWorker
import atexit

printer = PrinterWorker(adapter="hci0")
printer.start()
printer.connect()

@socketio.on("print")
def handle_print(data):
    printer.print_image(data["path"])   # reuses the live connection

atexit.register(printer.shutdown)
```

### Helper functions

| Function | Description |
|----------|-------------|
| `ensure_bluetooth_ready(adapter="hci0")` | Unblock + power on the Linux adapter (no-op off Linux) |
| `clear_cached_address()` | Drop the cached BLE address to force rediscovery |
| `load_cached_address()` / `save_cached_address(addr)` | Read/write the address cache |

## Finding Your Printer

```python
from niimbot_b1 import discover_printer
import asyncio

address = asyncio.run(discover_printer())
print(f"Found printer: {address}")
```

## Specs

- **Print width:** 384px (48mm)
- **DPI:** 203
- **Supported formats:** PNG, JPG, BMP, etc.

Images are automatically resized to 384px width and converted to 1-bit black/white. The image height determines print length—size your image to match your label dimensions.

So if you have a 768×480 image:
- Width: 768 → 384 (halved)
- Height: 480 → 240 (also halved, maintaining aspect ratio)
The height is scaled proportionally, not left as-is. So your image will look correct (not stretched/squished), but you still need to ensure your original image has the right aspect ratio for your label size.

## Example Script
This simple example demonstrates how to discover a NIIMBOT printer, establish a connection, and print a test image.
```
from niimbot_b1 import NiimbotB1, discover_printer
import asyncio

async def main():
    # ----- Find printer -----
    address = await discover_printer()
    if not address:
        print("No printer found")
        return
    
    print(f"Found: {address}")
    
    # -------- Print ----------
    async with NiimbotB1(address) as printer:
        print(f"Battery: {await printer.get_battery()}%")
        await printer.print_image("path/to/your/image.png")

asyncio.run(main())
```