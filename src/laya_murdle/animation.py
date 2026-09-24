"""Writing the deduction frames out as an animated GIF."""

from __future__ import annotations

import textwrap
from pathlib import Path

from laya_murdle.config import GifConfig


def write_gif(frames: list[tuple[str, str]], path: Path, gif: GifConfig = GifConfig()) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise SystemExit("--gif needs Pillow:\n  uv sync --extra gif") from exc

    try:
        font = ImageFont.truetype(gif.font, gif.font_size)
        bold = ImageFont.truetype(gif.font, gif.font_size, index=1)
    except OSError:
        font = bold = ImageFont.load_default()

    pages = []
    for title, grid in frames:
        wrapped = [
            line
            for paragraph in title.splitlines()
            for line in textwrap.wrap(paragraph, gif.wrap) or [""]
        ]
        pages.append((wrapped, grid.splitlines()))

    columns = max(len(line) for wrapped, grid in pages for line in wrapped + grid)
    rows = max(len(wrapped) + len(grid) + 1 for wrapped, grid in pages)
    box = font.getbbox("M")
    char_w = box[2] - box[0] + 1
    char_h = int((box[3] - box[1]) * gif.line_spacing)
    size = (columns * char_w + 2 * gif.margin, rows * char_h + 2 * gif.margin)

    images = []
    for wrapped, grid in pages:
        image = Image.new("RGB", size, gif.background)
        draw = ImageDraw.Draw(image)
        y = gif.margin
        for line in wrapped:
            draw.text((gif.margin, y), line, font=bold, fill=gif.title_color)
            y += char_h
        y += char_h // 2
        for line in grid:
            draw.text((gif.margin, y), line, font=font, fill=gif.grid_color)
            y += char_h
        images.append(image)

    frame_ms = int(gif.seconds * 1000)
    durations = [frame_ms] * (len(images) - 1) + [int(frame_ms * gif.final_hold)]
    path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(path, save_all=True, append_images=images[1:], duration=durations, loop=0)
