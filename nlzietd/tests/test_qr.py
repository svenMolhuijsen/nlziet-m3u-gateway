"""QR tests: the invariants a scanner relies on, and a pinned symbol.

A QR encoder is exactly the kind of code that looks finished while being wrong,
and the failure mode is silent: a picture that no phone will scan. Two layers of
protection here:

* Structure - size, finder eyes, timing, quiet zone - because those are what a
  decoder locates the symbol by.
* A digest of one complete matrix. That symbol was decoded by jsQR (an
  independent implementation) while this file was written; if the digest ever
  moves, decode a sample again before pasting a new one in. Do not update it
  just to make the test pass.
"""

from __future__ import annotations

import hashlib
import struct
import unittest
import zlib

from nlzietd.web import qr

SAMPLE = "https://id.nlziet.nl/device?user_code=ABC123"


class StructureTests(unittest.TestCase):
    def test_size_follows_the_version(self):
        # 21 modules at version 1, +4 per version. A pairing URL is 44 bytes,
        # which is version 4; level M holds 62 there.
        self.assertEqual(len(qr.matrix("a")), 21)
        self.assertEqual(len(qr.matrix("x" * 40)), 29)
        self.assertEqual(len(qr.matrix(SAMPLE)), 33)
        self.assertEqual(len(qr.matrix("x" * 80)), 37)

    def test_a_url_too_long_is_refused_rather_than_mangled(self):
        with self.assertRaises(ValueError):
            qr.matrix("x" * 120)

    def test_the_finder_eyes_are_where_a_scanner_looks(self):
        grid = qr.matrix(SAMPLE)
        size = len(grid)
        for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
            for row in range(7):
                for col in range(7):
                    ring = row in (0, 6) or col in (0, 6)
                    core = 2 <= row <= 4 and 2 <= col <= 4
                    self.assertEqual(
                        grid[top + row][left + col], ring or core,
                        f"eye at {top},{left} wrong at {row},{col}",
                    )

    def test_the_separators_stay_light(self):
        grid = qr.matrix(SAMPLE)
        size = len(grid)
        for col in range(8):
            self.assertFalse(grid[7][col])
        for row in range(8):
            self.assertFalse(grid[row][7])
        self.assertFalse(grid[7][size - 8])
        self.assertFalse(grid[size - 8][7])

    def test_the_timing_patterns_alternate(self):
        grid = qr.matrix(SAMPLE)
        for i in range(8, len(grid) - 8):
            self.assertEqual(grid[6][i], i % 2 == 0)
            self.assertEqual(grid[i][6], i % 2 == 0)

    def test_the_always_dark_module_is_dark(self):
        grid = qr.matrix(SAMPLE)
        self.assertTrue(grid[len(grid) - 8][8])

    def test_the_same_text_gives_the_same_image(self):
        self.assertEqual(qr.matrix(SAMPLE), qr.matrix(SAMPLE))

    def test_a_verified_symbol_is_pinned(self):
        grid = qr.matrix(SAMPLE)
        text = "\n".join(
            "".join("1" if dark else "0" for dark in row) for row in grid
        )
        self.assertEqual(
            hashlib.sha256(text.encode()).hexdigest(),
            "3501740e3c9f86bfa59fe9c436e9ceffb8dcdfe153a689dda872474deede88a0",
        )


class PngTests(unittest.TestCase):
    def test_it_is_a_png_with_the_expected_size(self):
        grid = qr.matrix(SAMPLE)
        image = qr.png(SAMPLE, scale=4, border=4)
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", image[16:24])
        expected = (len(grid) + 8) * 4
        self.assertEqual((width, height), (expected, expected))
        self.assertTrue(image.endswith(b"IEND\xaeB`\x82"))

    def test_the_quiet_zone_is_white_and_the_modules_are_black(self):
        # The quiet zone is what a scanner uses to find the symbol's edge; a
        # PNG that is white-on-black would have no quiet zone at all.
        image = qr.png("a", scale=4, border=4)
        raw = _raw_scanlines(image)
        width = (21 + 8) * 4
        self.assertEqual(raw[1:1 + width], b"\xff" * width)
        # The finder's top-left module: four quiet modules, four pixels each.
        self.assertEqual(raw[16 * (width + 1) + 16 + 1], 0x00)


def _raw_scanlines(image: bytes) -> bytes:
    """The decompressed pixel rows of our own 8-bit greyscale PNG."""
    position = 8
    payload = b""
    while position < len(image):
        length = struct.unpack(">I", image[position:position + 4])[0]
        tag = image[position + 4:position + 8]
        if tag == b"IDAT":
            payload += image[position + 8:position + 8 + length]
        position += 12 + length
    return zlib.decompress(payload)


if __name__ == "__main__":
    unittest.main()
