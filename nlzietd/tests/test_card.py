"""Card tests: the frame has to be a picture a phone can actually read.

The QR is the part that matters: a block of dark squares in the wrong place is
still a picture, it just scans to nothing. So instead of counting pixels, these
tests walk the matrix and check every module's colour in the finished frame,
plus the light quiet zone around it. The text is checked more loosely - it is
there, on a dark background - because its exact shapes are drawing, not data.
"""

from __future__ import annotations

import struct
import unittest
import zlib

from nlzietd.web import card, qr

URL = "https://id.nlziet.nl/device?user_code=ABC123"
CODE = "ABC123"
PAGE = "https://id.nlziet.nl/device"


def decode(image: bytes) -> tuple[int, int, list[bytes]]:
    """Width, height and the greyscale rows of one of our own PNGs."""
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    position = 8
    width = height = 0
    payload = b""
    while position < len(image):
        length = struct.unpack(">I", image[position:position + 4])[0]
        tag = image[position + 4:position + 8]
        body = image[position + 8:position + 8 + length]
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            assert (depth, colour) == (8, 0), "8-bit greyscale expected"
        elif tag == b"IDAT":
            payload += body
        position += 12 + length
    raw = zlib.decompress(payload)
    rows = []
    for row in range(height):
        at = row * (width + 1)
        assert raw[at] == 0, "filter 0 expected"
        rows.append(raw[at + 1:at + 1 + width])
    return width, height, rows


def layout():
    """The same numbers the card uses, recomputed for the assertions."""
    grid = qr.matrix(URL)
    scale = max(2, min(14, 420 // (len(grid) + 8)))
    tile = (len(grid) + 8) * scale
    top = 140 + max(0, (720 - 200 - tile) // 2)
    return grid, scale, tile, 88, top


class CardTests(unittest.TestCase):
    def test_it_is_a_16_9_greyscale_png(self):
        width, height, _rows = decode(card.pairing_card(URL, CODE, PAGE))
        self.assertEqual((width, height), (1280, 720))

    def test_every_qr_module_is_drawn_in_the_right_colour(self):
        _, _, rows = decode(card.pairing_card(URL, CODE, PAGE))
        grid, scale, _tile, left, top = layout()
        for row in range(len(grid)):
            for col in range(len(grid)):
                x = left + (4 + col) * scale + scale // 2
                y = top + (4 + row) * scale + scale // 2
                expected = 0 if grid[row][col] else 255
                self.assertEqual(rows[y][x], expected, f"module {row},{col}")

    def test_the_quiet_zone_around_the_symbol_is_white(self):
        _, _, rows = decode(card.pairing_card(URL, CODE, PAGE))
        _grid, scale, tile, left, top = layout()
        for x, y in (
            (left + scale // 2, top + scale // 2),
            (left + tile - scale // 2, top + scale // 2),
            (left + scale // 2, top + tile - scale // 2),
            (left + tile - scale // 2, top + tile - scale // 2),
        ):
            self.assertEqual(rows[y][x], 255, f"quiet zone at {x},{y}")

    def test_the_background_is_dark_and_the_text_is_light(self):
        _, _, rows = decode(card.pairing_card(URL, CODE, PAGE))
        # Header band, well away from the QR tile.
        band = rows[52:87]
        self.assertEqual(band[0][10], 14)
        self.assertGreater(sum(1 for row in band for px in row if px > 200), 100)

    def test_the_code_is_drawn_large(self):
        _grid, _scale, _tile, _left, top = layout()
        _, _, rows = decode(card.pairing_card(URL, CODE, PAGE))
        band = rows[top + 74:top + 74 + 98]
        light = sum(1 for row in band for px in row if px > 200)
        # Six glyphs at 14 px/module is thousands of lit pixels; a missing
        # font table or an off-screen column would leave this at zero.
        self.assertGreater(light, 2000)

    def test_the_page_drops_the_scheme(self):
        self.assertEqual(card.display_page(PAGE), "ID.NLZIET.NL/DEVICE")
        self.assertEqual(
            card.display_page("http://tv.local:8099/device?x=1"),
            "TV.LOCAL:8099/DEVICE?X=1",
        )

    def test_a_character_we_do_not_have_is_a_gap_not_a_crash(self):
        image = card.pairing_card(URL, "K?F6Q1", "https://host/_")
        width, height, _rows = decode(image)
        self.assertEqual((width, height), (1280, 720))

    def test_a_long_host_still_produces_a_frame(self):
        image = card.pairing_card(
            URL, CODE, "https://a-very-long-hostname.example.test/device"
        )
        width, height, _rows = decode(image)
        self.assertEqual((width, height), (1280, 720))


if __name__ == "__main__":
    unittest.main()
