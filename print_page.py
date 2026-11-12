import argparse
import struct
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright


def capture_page(url, output):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 576, "height": 800}, device_scale_factor=1)
        page.goto(url, wait_until="load")
        page.evaluate("document.fonts.ready")
        page.evaluate("""async () => {
            await Promise.all(Array.from(document.images, image => image.decode()));
        }""")
        page.screenshot(path=str(output), full_page=True, animations="disabled")
        browser.close()


def encode_receipt(screenshot, output):
    with Image.open(screenshot) as source:
        if source.width != 576:
            raise ValueError("Expected a 576-pixel-wide screenshot")
        image = source.convert("L").point(lambda pixel: 255 if pixel >= 160 else 0, mode="1")
    with output.open("wb") as receipt:
        receipt.write(b"\x1b@")
        for top in range(0, image.height, 256):
            strip = image.crop((0, top, image.width, min(top + 256, image.height)))
            raster = bytes(byte ^ 255 for byte in strip.tobytes())
            receipt.write(b"\x1dv0\x00" + struct.pack("<HH", image.width // 8, strip.height))
            receipt.write(raster)
        receipt.write(b"\x1bd\x03\x1dVB\x00")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("page", help="HTML file path or URL")
    parser.add_argument("--output", type=Path, default=Path("newspaper.png"))
    args = parser.parse_args()
    url = args.page if args.page.startswith(("http://", "https://", "file://")) else Path(args.page).resolve().as_uri()
    capture_page(url, args.output)
    receipt = args.output.with_suffix(".escpos")
    encode_receipt(args.output, receipt)
    print(f"Saved screenshot: {args.output}")
    print(f"Saved printer data: {receipt}")


if __name__ == "__main__":
    main()
