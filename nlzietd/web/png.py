"""PNG writing for the two pictures this daemon draws itself.

A QR symbol and the pairing card around it are the only images here, and
neither is worth a dependency: PIL would pull a compiled library into a
daemon that otherwise needs nothing but Python, for two flat greyscale pictures
a few dozen lines can write.

Why greyscale and no filter: both images are flat colour and blocky text, so a
predictor buys a few kilobytes and costs a class of bug that only shows up on a
TV you cannot connect a debugger to. A zlib run over flat rows is already
small.
"""

from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def png(width: int, height: int, samples: bytes) -> bytes:
    """Wrap `width * height` 8-bit greyscale samples in a PNG container."""
    if len(samples) != width * height:
        raise ValueError(f"{len(samples)} samples for a {width}x{height} image")
    raw = bytearray()
    for row in range(height):
        raw += b"\x00"  # filter: none
        raw += samples[row * width:(row + 1) * width]
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


class Canvas:
    """A mutable greyscale image: rectangles in, PNG out.

    `fill` is the only drawing primitive. Everything on the card is a
    rectangle - text glyphs are made of them - so a line or circle routine
    would be code nobody calls.
    """

    def __init__(self, width: int, height: int, shade: int = 0) -> None:
        self.width = width
        self.height = height
        self.samples = bytearray([shade]) * (width * height)

    def fill(self, x: int, y: int, w: int, h: int, shade: int) -> None:
        # Clipped rather than validated: a layout that overshoots the edge by a
        # pixel should still produce a picture, not an exception on a TV.
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes([shade]) * (x1 - x0)
        for py in range(y0, y1):
            at = py * self.width
            self.samples[at + x0:at + x1] = row

    def stamp(self, grid: list[list[bool]], x: int, y: int, scale: int = 1) -> None:
        """Draw a boolean grid, dark cells as black, with `scale` px per cell."""
        for row_index, row in enumerate(grid):
            for col_index, dark in enumerate(row):
                if dark:
                    self.fill(
                        x + col_index * scale, y + row_index * scale,
                        scale, scale, 0,
                    )

    def png(self) -> bytes:
        return png(self.width, self.height, self.samples)


def modules(grid: list[list[bool]], scale: int = 1, border: int = 0) -> bytes:
    """A QR grid as a PNG: dark modules on white, `border` quiet modules."""
    size = (len(grid) + 2 * border) * scale
    canvas = Canvas(size, size, 255)
    canvas.stamp(grid, border * scale, border * scale, scale)
    return canvas.png()
