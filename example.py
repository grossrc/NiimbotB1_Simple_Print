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
        await printer.print_image("image.png")

asyncio.run(main())