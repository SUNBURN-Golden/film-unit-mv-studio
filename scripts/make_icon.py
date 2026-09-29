"""Draw the app icon (no fonts, no downloads) as PNG, ICO and ICNS for the installers."""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024
INK, PAPER, TEAL = "#20221F", "#EEEAE3", "#287D76"


def draw():
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((32, 32, SIZE - 32, SIZE - 32), radius=200, fill=INK)
    # A play triangle over four level bars: a song turned into a picture.
    d.polygon([(400, 300), (400, 700), (760, 500)], fill=TEAL)
    for i, height in enumerate((120, 220, 160, 90)):
        x = 150 + i * 62
        d.rounded_rectangle((x, 720 - height, x + 34, 720), radius=10, fill=PAPER)
    return image


def main(target=ROOT / "vendor/icon"):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    image = draw()
    image.save(target / "icon.png")
    image.resize((256, 256), Image.LANCZOS).save(target / "icon-256.png")  # Linux menu icon
    image.save(target / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    image.save(target / "icon.icns")
    return sorted(p.name for p in target.iterdir())


if __name__ == "__main__":
    print(main())
