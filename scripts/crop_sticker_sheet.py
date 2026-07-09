from pathlib import Path

from PIL import Image


SHEET = Path("assets/stickers/rin-sticker-sheet-v1.png")
OUTPUTS = [
    "rin-happy.png",
    "rin-sulk.png",
    "rin-miss-you.png",
    "rin-jealous-soft.png",
    "rin-angry-soft.png",
    "rin-sleepy.png",
    "rin-comfort.png",
    "rin-teasing.png",
]


def main() -> None:
    image = Image.open(SHEET)
    width, height = image.size
    cell_width = width // 4
    cell_height = height // 2
    output_dir = SHEET.parent

    for index, filename in enumerate(OUTPUTS):
        row = index // 4
        col = index % 4
        left = col * cell_width
        upper = row * cell_height
        right = width if col == 3 else (col + 1) * cell_width
        lower = height if row == 1 else (row + 1) * cell_height
        crop = image.crop((left, upper, right, lower))
        crop.save(output_dir / filename)


if __name__ == "__main__":
    main()
