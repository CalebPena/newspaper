import struct
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    started = time.perf_counter()
    fonts = Path("/usr/share/fonts/truetype/liberation")
    if not fonts.exists():
        fonts = Path("/usr/share/fonts/truetype/liberation2")
    body = ImageFont.truetype(str(fonts / "LiberationSerif-Regular.ttf"), 26)
    title = ImageFont.truetype(str(fonts / "LiberationSerif-Bold.ttf"), 44)
    heading = ImageFont.truetype(str(fonts / "LiberationSerif-Bold.ttf"), 34)
    image = Image.new("1", (576, 1800), 1)
    draw = ImageDraw.Draw(image)
    y = 24

    def paragraph(text, font, spacing=10):
        nonlocal y
        line = ""
        for word in text.split():
            candidate = f"{line} {word}".strip()
            if line and draw.textlength(candidate, font=font) > 528:
                draw.text((24, y), line, font=font, fill=0)
                y += font.size + 10
                line = word
            else:
                line = candidate
        if line:
            draw.text((24, y), line, font=font, fill=0)
            y += font.size + 10
        y += spacing

    def divider():
        nonlocal y
        draw.line((24, y, 552, y), fill=0, width=2)
        y += 24

    paragraph("THE MORNING ROLL", title)
    paragraph("The Pillow edition | No. 002", body)
    divider()
    paragraph("No browser required", heading)
    paragraph("This newspaper was drawn directly on the Raspberry Pi using Pillow. No Chrome. No HTML. Just text, lines, and pixels.", body)
    center = y + 80
    draw.arc((218, center - 55, 358, center + 85), 180, 360, fill=0, width=4)
    draw.line((48, center + 15, 528, center + 15), fill=0, width=3)
    for x in range(72, 505, 48):
        draw.line((x, center + 40, x + 24, center + 56), fill=0, width=3)
    y += 170
    divider()
    paragraph("A simpler morning", heading)
    paragraph("Headlines, daily notes, weather, and small illustrations can all be composed without launching a web browser.", body)
    paragraph("Pour your coffee. Your paper is ready.", body)
    divider()
    paragraph("End of edition", body)
    image = image.crop((0, 0, 576, y + 24))
    output = Path("/home/diesel/pillow-benchmark.png")
    image.save(output)
    rendered = time.perf_counter()
    receipt = output.with_suffix(".escpos")
    with receipt.open("wb") as stream:
        stream.write(b"\x1b@")
        for top in range(0, image.height, 256):
            strip = image.crop((0, top, 576, min(top + 256, image.height)))
            stream.write(b"\x1dv0\x00" + struct.pack("<HH", 72, strip.height))
            stream.write(bytes(byte ^ 255 for byte in strip.tobytes()))
        stream.write(b"\x1bd\x03\x1dVB\x00")
    print(f"Image generation and PNG save: {rendered - started:.3f} seconds")
    print(f"Total including ESC/POS conversion: {time.perf_counter() - started:.3f} seconds")
    print(f"Image size: {image.width} x {image.height}")
    print(f"Printer data: {receipt}")


if __name__ == "__main__":
    main()
