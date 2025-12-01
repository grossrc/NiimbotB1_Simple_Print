"""
NiimBot B1 Printer Library

A clean implementation based on the protocol documentation at:
https://printers.niim.blue/interfacing/proto/
https://printers.niim.blue/interfacing/print-tasks/

B1 Specifications:
- DPI: 203
- Printhead size: 48mm (384px)
- Paper types: 1 (Gap), 2 (Black), 5 (Transparent)
- Density range: 1-5 (default 3)
"""

import asyncio
import struct
import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Callable
from bleak import BleakClient, BleakScanner
from PIL import Image, ImageOps, ImageDraw, ImageFont


# ============================================================================
# Constants
# ============================================================================

# B1 Printer Specifications
PRINTER_WIDTH_PX = 384  # 48mm * 8 dots/mm = 384 pixels
PRINTER_DPI = 203

# BLE UUIDs - Custom service (single characteristic for read/write/notify)
UUID_SERVICE_CUSTOM = "e7810a71-73ae-499d-8c15-faa9aef0c3f2"
UUID_CHAR_CUSTOM = "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f"

# ISSC Transparent UART Service UUIDs
UUID_SERVICE_ISSC = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
UUID_ISSC_NOTIFY = "49535343-1e4d-4bd9-ba61-23c647249616"
UUID_ISSC_WRITE = "49535343-8841-43f4-a8d4-ecbe34729bb3"
UUID_ISSC_WRITE_NO_RESP = "49535343-6daa-4d02-abf6-19569aca69fe"


# ============================================================================
# Protocol Enums
# ============================================================================

class Command(IntEnum):
    """NiimBot Protocol Commands"""
    PRINT_START = 0x01
    PAGE_START = 0x03
    SET_PAGE_SIZE = 0x13
    PRINT_QUANTITY = 0x15
    RFID_INFO = 0x1A
    PRINT_CLEAR = 0x20
    SET_DENSITY = 0x21
    SET_LABEL_TYPE = 0x23
    PRINTER_INFO = 0x40
    PRINT_BITMAP_ROW_INDEXED = 0x83
    PRINT_EMPTY_ROW = 0x84
    PRINT_BITMAP_ROW = 0x85
    CALIBRATE = 0x8E
    PRINT_STATUS = 0xA3
    CONNECT = 0xC1
    PAGE_END = 0xE3
    PRINT_END = 0xF3


class LabelType(IntEnum):
    """Paper/Label Types"""
    WITH_GAPS = 1
    BLACK = 2
    CONTINUOUS = 3
    PERFORATED = 4
    TRANSPARENT = 5
    PVC_TAG = 6
    BLACK_MARK_GAP = 10
    HEAT_SHRINK_TUBE = 11


class PrinterInfoType(IntEnum):
    """Printer Info Sub-commands"""
    DENSITY = 0x01
    PRINT_SPEED = 0x02
    LABEL_TYPE = 0x03
    LANGUAGE = 0x06
    AUTO_SHUTDOWN = 0x07
    DEVICE_TYPE = 0x08
    SOFTWARE_VERSION = 0x09
    BATTERY = 0x0A
    DEVICE_SERIAL = 0x0B
    HARDWARE_VERSION = 0x0C


# ============================================================================
# Packet Class
# ============================================================================

@dataclass
class NiimbotPacket:
    """
    NiimBot Protocol Packet
    
    Structure:
    - Head: 0x55 0x55
    - Command: 1 byte
    - Data Length: 1 byte
    - Data: variable
    - Checksum: XOR of Command, Length, and all Data bytes
    - Tail: 0xAA 0xAA
    """
    command: int
    data: bytes = b''
    
    def to_bytes(self) -> bytes:
        """Serialize packet to bytes"""
        packet = bytearray([0x55, 0x55])
        packet.append(self.command)
        packet.append(len(self.data))
        packet.extend(self.data)
        
        # Calculate checksum: XOR of command, length, and all data bytes
        checksum = self.command ^ len(self.data)
        for byte in self.data:
            checksum ^= byte
        
        packet.append(checksum)
        packet.extend([0xAA, 0xAA])
        return bytes(packet)
    
    @classmethod
    def from_bytes(cls, data: bytes) -> Optional['NiimbotPacket']:
        """Parse packet from bytes"""
        if len(data) < 7:
            return None
        if data[:2] != b'\x55\x55':
            return None
        if data[-2:] != b'\xaa\xaa':
            return None
        
        command = data[2]
        length = data[3]
        
        if len(data) != 4 + length + 3:
            return None
        
        pkt_data = data[4:4+length]
        
        # Verify checksum
        expected_checksum = command ^ length
        for byte in pkt_data:
            expected_checksum ^= byte
        
        actual_checksum = data[4+length]
        if expected_checksum != actual_checksum:
            return None
        
        return cls(command, pkt_data)
    
    def __repr__(self):
        return f"NiimbotPacket(cmd=0x{self.command:02X}, data={self.data.hex()})"


# ============================================================================
# Printer Client
# ============================================================================

class NiimbotB1:
    """
    NiimBot B1 Printer Client
    
    Usage:
        async with NiimbotB1("XX:XX:XX:XX:XX:XX") as printer:
            await printer.print_image("test.png")
    """
    
    def __init__(self, address: str, density: int = 3, label_type: LabelType = LabelType.WITH_GAPS):
        self.address = address
        self.density = max(1, min(5, density))  # Clamp to 1-5
        self.label_type = label_type
        
        self.client: Optional[BleakClient] = None
        self.write_uuid: Optional[str] = None
        self.notify_uuid: Optional[str] = None
        
        self._response_queue: asyncio.Queue = asyncio.Queue()
        self._recv_buffer: bytearray = bytearray()
        self._debug = True
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, *args):
        await self.disconnect()
    
    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------
    
    async def connect(self):
        """Connect to the printer via BLE"""
        self._log(f"Connecting to {self.address}...")
        
        self.client = BleakClient(self.address)
        await self.client.connect()
        
        # Discover services and find correct UUIDs
        await self._discover_uuids()
        
        # Start notification handler
        await self.client.start_notify(self.notify_uuid, self._notification_handler)
        
        self._log("Connected successfully")
        
        # Send connect command
        await self._send_connect()
    
    async def disconnect(self):
        """Disconnect from the printer"""
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(self.notify_uuid)
            except Exception:
                pass
            await self.client.disconnect()
            self._log("Disconnected")
    
    async def _discover_uuids(self):
        """Discover the correct write and notify UUIDs"""
        self._log("Discovering services...")
        
        for service in self.client.services:
            self._log(f"  Service: {service.uuid}")
            for char in service.characteristics:
                props = char.properties
                self._log(f"    Char: {char.uuid} [{', '.join(props)}]")
        
        # Try custom service first (single characteristic for everything)
        try:
            char = self.client.services.get_characteristic(UUID_CHAR_CUSTOM)
            if char and 'write-without-response' in char.properties and 'notify' in char.properties:
                self.write_uuid = UUID_CHAR_CUSTOM
                self.notify_uuid = UUID_CHAR_CUSTOM
                self._log(f"Using custom service (single characteristic)")
                return
        except Exception:
            pass
        
        # Try ISSC service (separate characteristics)
        try:
            notify_char = self.client.services.get_characteristic(UUID_ISSC_NOTIFY)
            write_char = self.client.services.get_characteristic(UUID_ISSC_WRITE_NO_RESP)
            if notify_char and write_char:
                self.write_uuid = UUID_ISSC_WRITE_NO_RESP
                self.notify_uuid = UUID_ISSC_NOTIFY
                self._log(f"Using ISSC service (separate characteristics)")
                return
        except Exception:
            pass
        
        # Fallback: try ISSC write with response
        try:
            notify_char = self.client.services.get_characteristic(UUID_ISSC_NOTIFY)
            write_char = self.client.services.get_characteristic(UUID_ISSC_WRITE)
            if notify_char and write_char:
                self.write_uuid = UUID_ISSC_WRITE
                self.notify_uuid = UUID_ISSC_NOTIFY
                self._log(f"Using ISSC service (write with response)")
                return
        except Exception:
            pass
        
        # Last resort: search for any suitable characteristics
        for service in self.client.services:
            for char in service.characteristics:
                if 'write-without-response' in char.properties or 'write' in char.properties:
                    if not self.write_uuid:
                        self.write_uuid = char.uuid
                if 'notify' in char.properties:
                    if not self.notify_uuid:
                        self.notify_uuid = char.uuid
        
        if not self.write_uuid or not self.notify_uuid:
            raise RuntimeError("Could not find suitable BLE characteristics")
        
        self._log(f"Using discovered UUIDs: write={self.write_uuid}, notify={self.notify_uuid}")
    
    def _notification_handler(self, sender, data: bytes):
        """Handle incoming BLE notifications"""
        self._recv_buffer.extend(data)
        self._parse_packets()
    
    def _parse_packets(self):
        """Parse complete packets from the receive buffer"""
        while len(self._recv_buffer) >= 7:
            # Look for packet header
            if self._recv_buffer[:2] != b'\x55\x55':
                # Skip garbage byte
                del self._recv_buffer[0]
                continue
            
            # Check if we have enough data
            if len(self._recv_buffer) < 4:
                break
            
            length = self._recv_buffer[3]
            packet_size = 4 + length + 3  # header(2) + cmd(1) + len(1) + data(len) + checksum(1) + tail(2)
            
            if len(self._recv_buffer) < packet_size:
                break
            
            # Extract and parse packet
            packet_data = bytes(self._recv_buffer[:packet_size])
            del self._recv_buffer[:packet_size]
            
            packet = NiimbotPacket.from_bytes(packet_data)
            if packet:
                self._log(f"RX: {packet}")
                self._response_queue.put_nowait(packet)
    
    # -------------------------------------------------------------------------
    # Low-level Communication
    # -------------------------------------------------------------------------
    
    async def _send(self, command: int, data: bytes = b''):
        """Send a packet to the printer"""
        packet = NiimbotPacket(command, data)
        self._log(f"TX: {packet}")
        await self.client.write_gatt_char(self.write_uuid, packet.to_bytes(), response=False)
    
    async def _recv(self, expected_command: int, timeout: float = 2.0) -> Optional[NiimbotPacket]:
        """Wait for a response packet"""
        try:
            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < timeout:
                try:
                    packet = await asyncio.wait_for(self._response_queue.get(), timeout=0.1)
                    if packet.command == expected_command:
                        return packet
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            self._log(f"Receive error: {e}")
        return None
    
    async def _transceive(self, command: int, data: bytes = b'', response_offset: int = 0x10) -> Optional[NiimbotPacket]:
        """Send a command and wait for response"""
        await self._send(command, data)
        await asyncio.sleep(0.05)  # Small delay for processing
        
        # Response command is typically request + offset (varies by command)
        response_cmd = command + response_offset
        return await self._recv(response_cmd)
    
    # -------------------------------------------------------------------------
    # Printer Commands
    # -------------------------------------------------------------------------
    
    async def _send_connect(self):
        """Send connect command (0xC1)"""
        # Connect packet uses prefix 0x03 according to wiki
        # But most implementations just send 0xC1 with data 0x01
        await self._send(Command.CONNECT, b'\x01')
        response = await self._recv(0xC2, timeout=1.0)
        if response:
            self._log(f"Connect response: {response.data.hex()}")
    
    async def get_info(self, info_type: PrinterInfoType) -> Optional[bytes]:
        """Get printer information"""
        await self._send(Command.PRINTER_INFO, bytes([info_type]))
        # Response codes vary: 0x4F, 0x47, 0x4D, 0x4A, 0x41, 0x4L, 0x43, 0x46, 0x48, 0x4B, 0x49, 0x42
        # Wait for any response starting with 0x4X
        for _ in range(10):
            try:
                packet = await asyncio.wait_for(self._response_queue.get(), timeout=0.2)
                if 0x40 <= packet.command <= 0x4F:
                    return packet.data
            except asyncio.TimeoutError:
                break
        return None
    
    async def get_battery(self) -> Optional[int]:
        """Get battery percentage"""
        data = await self.get_info(PrinterInfoType.BATTERY)
        if data and len(data) >= 1:
            return data[0]
        return None
    
    async def get_rfid(self) -> Optional[dict]:
        """
        Get label roll RFID info
        
        Returns dict with:
            - uuid: Roll identifier
            - width_mm: Label width in mm
            - height_mm: Label height in mm  
            - remaining: Estimated labels remaining
        Returns None if no RFID data available
        """
        await self._send(Command.RFID_INFO, b'\x01')
        response = await self._recv(0x1B, timeout=1.0)
        if response and len(response.data) >= 8:
            data = response.data
            # Parse RFID response based on protocol
            # Format varies but typically includes dimensions and count
            try:
                return {
                    'uuid': data[:4].hex(),
                    'width_mm': data[4] if len(data) > 4 else None,
                    'height_mm': data[5] if len(data) > 5 else None,
                    'remaining': struct.unpack(">H", data[6:8])[0] if len(data) >= 8 else None
                }
            except:
                return {'raw': data.hex()}
        return None
    
    async def calibrate(self) -> bool:
        """
        Calibrate the label sensor
        
        Run this after changing label rolls to ensure proper positioning.
        Returns True if calibration succeeded.
        """
        self._log("Calibrating label sensor...")
        await self._send(Command.CALIBRATE, b'\x01')
        response = await self._recv(0x8F, timeout=5.0)
        success = response and len(response.data) > 0 and response.data[0] == 1
        self._log(f"Calibration {'succeeded' if success else 'failed'}")
        return success
    
    async def get_print_status(self) -> Optional[tuple]:
        """Get print status - returns (page, progress, total)"""
        await self._send(Command.PRINT_STATUS, b'\x01')
        response = await self._recv(0xB3, timeout=1.0)
        if response and len(response.data) >= 4:
            page = struct.unpack(">H", response.data[0:2])[0]
            progress = response.data[2]
            total = response.data[3]
            return (page, progress, total)
        return None
    
    async def set_density(self, density: int):
        """Set print density (1-5)"""
        density = max(1, min(5, density))
        await self._send(Command.SET_DENSITY, bytes([density]))
        response = await self._recv(0x31, timeout=1.0)
        return response and len(response.data) > 0 and response.data[0] == 1
    
    async def set_label_type(self, label_type: LabelType):
        """Set label/paper type"""
        await self._send(Command.SET_LABEL_TYPE, bytes([label_type]))
        response = await self._recv(0x33, timeout=1.0)
        return response and len(response.data) > 0 and response.data[0] == 1
    
    async def print_start(self, total_pages: int = 1):
        """
        Start print job
        
        B1 uses 7-byte format:
        - Total pages (2 bytes, big-endian)
        - Always 0 (4 bytes)
        - Page color (1 byte)
        """
        data = struct.pack(">H", total_pages) + b'\x00\x00\x00\x00\x00'
        await self._send(Command.PRINT_START, data)
        response = await self._recv(0x02, timeout=1.0)
        return response
    
    async def page_start(self):
        """Start a new page"""
        await self._send(Command.PAGE_START, b'\x01')
        response = await self._recv(0x04, timeout=1.0)
        return response
    
    async def set_page_size(self, rows: int, cols: int, copies: int = 1):
        """
        Set page dimensions
        
        B1 uses 6-byte format:
        - Rows (2 bytes, big-endian)
        - Cols (2 bytes, big-endian)
        - Copies (2 bytes, big-endian)
        """
        data = struct.pack(">HHH", rows, cols, copies)
        await self._send(Command.SET_PAGE_SIZE, data)
        response = await self._recv(0x14, timeout=1.0)
        return response
    
    async def send_bitmap_row(self, row_number: int, row_data: bytes, repeat: int = 1):
        """
        Send a bitmap row
        
        Format:
        - Row number (2 bytes, big-endian)
        - Black pixel count (3 bytes - for split mode, or total)
        - Repeat count (1 byte)
        - Pixel data
        """
        # Count black pixels (bits set to 1)
        black_count = sum(bin(b).count('1') for b in row_data)
        
        # Use total mode: [0, low_byte, high_byte]
        black_count_bytes = bytes([0, black_count & 0xFF, (black_count >> 8) & 0xFF])
        
        header = struct.pack(">H", row_number) + black_count_bytes + bytes([repeat])
        data = header + row_data
        
        await self._send(Command.PRINT_BITMAP_ROW, data)
    
    async def send_empty_row(self, row_number: int, repeat: int = 1):
        """Send an empty (white) row"""
        data = struct.pack(">H", row_number) + bytes([repeat])
        await self._send(Command.PRINT_EMPTY_ROW, data)
    
    async def page_end(self):
        """End current page"""
        await self._send(Command.PAGE_END, b'\x01')
        response = await self._recv(0xE4, timeout=1.0)
        return response
    
    async def print_end(self):
        """End print job"""
        await self._send(Command.PRINT_END, b'\x01')
        response = await self._recv(0xF4, timeout=2.0)
        return response
    
    async def wait_for_print_complete(self, timeout: float = 30.0) -> bool:
        """Wait for print to complete by polling status"""
        start = asyncio.get_event_loop().time()
        
        while asyncio.get_event_loop().time() - start < timeout:
            status = await self.get_print_status()
            if status:
                page, progress, total = status
                self._log(f"Print status: page={page}, progress={progress}%, total={total}")
                if progress >= 100:
                    return True
            await asyncio.sleep(0.5)
        
        return False
    
    # -------------------------------------------------------------------------
    # Image Printing
    # -------------------------------------------------------------------------
    
    def prepare_image(self, image_path: str, target_width: int = PRINTER_WIDTH_PX) -> Image.Image:
        """
        Prepare an image for printing
        
        - Resize to printer width
        - Convert to 1-bit black/white
        - Invert so black pixels are 1 (burn)
        """
        img = Image.open(image_path)
        
        # Resize maintaining aspect ratio
        if img.width != target_width:
            ratio = target_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((target_width, new_height), Image.Resampling.LANCZOS)
        
        # Convert to grayscale, then invert (so black becomes white = 255)
        img = img.convert('L')
        img = ImageOps.invert(img)
        
        # Convert to 1-bit (255 -> 1, 0 -> 0)
        img = img.convert('1')
        
        return img
    
    def image_to_rows(self, img: Image.Image) -> list:
        """
        Convert image to list of row data
        
        Returns list of tuples: (row_bytes, is_empty)
        """
        rows = []
        width, height = img.size
        
        for y in range(height):
            row_img = img.crop((0, y, width, y + 1))
            row_bytes = row_img.tobytes()
            
            # Check if row is empty (all zeros = all white)
            is_empty = all(b == 0 for b in row_bytes)
            rows.append((row_bytes, is_empty))
        
        return rows
    
    async def print_image(self, image_path: str, copies: int = 1) -> bool:
        """
        Print an image
        
        Args:
            image_path: Path to image file
            copies: Number of copies to print
        
        Returns:
            True if print completed successfully
        """
        self._log(f"Preparing image: {image_path}")
        
        # Prepare image
        img = self.prepare_image(image_path)
        width, height = img.size
        self._log(f"Image size: {width}x{height}")
        
        # Get row data
        rows = self.image_to_rows(img)
        
        # Print sequence for B1 (from wiki)
        self._log("Starting print job...")
        
        # 1. Set density
        await self.set_density(self.density)
        await asyncio.sleep(0.1)
        
        # 2. Set label type
        await self.set_label_type(self.label_type)
        await asyncio.sleep(0.1)
        
        # 3. Print start
        await self.print_start(total_pages=copies)
        await asyncio.sleep(0.1)
        
        # 4. Page start
        await self.page_start()
        await asyncio.sleep(0.1)
        
        # 5. Set page size (rows, cols, copies)
        await self.set_page_size(height, width, copies)
        await asyncio.sleep(0.1)
        
        # 6. Send image data
        self._log(f"Sending {height} rows of image data...")
        
        empty_count = 0
        for y, (row_data, is_empty) in enumerate(rows):
            if is_empty:
                # Accumulate empty rows
                empty_count += 1
            else:
                # Send accumulated empty rows
                if empty_count > 0:
                    await self.send_empty_row(y - empty_count, repeat=empty_count)
                    empty_count = 0
                
                # Send bitmap row
                await self.send_bitmap_row(y, row_data, repeat=1)
            
            # Flow control - small delay every 20 rows
            if y % 20 == 0:
                await asyncio.sleep(0.01)
                if y % 100 == 0:
                    self._log(f"  Progress: {y}/{height} rows")
        
        # Send any remaining empty rows
        if empty_count > 0:
            await self.send_empty_row(height - empty_count, repeat=empty_count)
        
        self._log("Image data sent")
        
        # 7. Page end
        await self.page_end()
        await asyncio.sleep(0.1)
        
        # 8. Wait for print to complete
        self._log("Waiting for print to complete...")
        await asyncio.sleep(2.0)  # Give printer time to process
        
        # Poll status
        success = await self.wait_for_print_complete(timeout=30.0)
        
        # 9. Print end
        await self.print_end()
        
        self._log(f"Print {'completed successfully' if success else 'may have failed'}")
        return success
    
    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------
    
    def _log(self, message: str):
        """Log a debug message"""
        if self._debug:
            print(f"[NiimbotB1] {message}")


# ============================================================================
# Helper Functions
# ============================================================================

async def discover_printer() -> Optional[str]:
    """Scan for NiimBot printers and return the address"""
    print("Scanning for NiimBot printers...")
    
    devices = await BleakScanner.discover(timeout=5.0)
    
    for device in devices:
        name = device.name or ""
        if "NIIMBOT" in name.upper() or "B1" in name.upper():
            print(f"Found: {device.name} ({device.address})")
            return device.address
    
    print("No NiimBot printer found")
    return None


def create_test_image(path: str, width: int = 384, height: int = 240, text: str = "TEST"):
    """Create a simple test image for printing"""
    img = Image.new('RGB', (width, height), color='white')
    draw = ImageDraw.Draw(img)
    
    # Draw border
    draw.rectangle([2, 2, width-3, height-3], outline='black', width=2)
    
    # Draw text
    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except:
        font = ImageFont.load_default()
    
    # Center the text
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (width - text_width) // 2
    y = (height - text_height) // 2
    
    draw.text((x, y), text, fill='black', font=font)
    
    # Draw some shapes
    draw.rectangle([20, 20, 60, 60], outline='black', width=2)
    draw.ellipse([width-60, 20, width-20, 60], outline='black', width=2)
    draw.rectangle([20, height-60, 60, height-20], fill='black')
    draw.ellipse([width-60, height-60, width-20, height-20], fill='black')
    
    img.save(path)
    print(f"Test image created: {path}")
    return path


def mm_to_pixels(mm: float, dpi: int = PRINTER_DPI) -> int:
    """Convert millimeters to pixels at given DPI"""
    return int(mm * dpi / 25.4)


# ============================================================================
# Main
# ============================================================================

async def main():
    """Main function for testing"""
    # Known printer address
    PRINTER_ADDRESS = "04:09:12:59:21:67"
    
    # Create test image for 50x30mm label
    # At 203 DPI: 50mm = 400px (but printer is 384px wide), 30mm = 240px
    # Width is limited by printhead (384px = 48mm)
    label_width_px = PRINTER_WIDTH_PX  # 384
    label_height_px = mm_to_pixels(30)  # ~240
    
    test_image = "TestPrint.png"
    create_test_image(test_image, label_width_px, label_height_px, "NIIMBOT B1")
    
    try:
        # Connect and print
        async with NiimbotB1(
            PRINTER_ADDRESS,
            density=3,
            label_type=LabelType.WITH_GAPS
        ) as printer:
            # Get battery level
            battery = await printer.get_battery()
            if battery:
                print(f"Battery: {battery}%")
            
            # Print the test image
            success = await printer.print_image(test_image, copies=1)
            
            if success:
                print("Print completed successfully!")
            else:
                print("Print may have encountered issues")
                
    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
