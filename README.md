# NiimBot B1 Label Printer Library

A Python library for interfacing with NiimBot B1 thermal label printers via Bluetooth LE. Requires only two dependencies: `bleak` for BLE communication and `Pillow` for image processing—no bloated SDKs or proprietary tools needed.

## Installation

```bash
pip install -r requirements.txt
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