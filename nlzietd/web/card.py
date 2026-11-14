"""The pairing card: one frame of the video a TV can show.

An M3U channel is a name, a logo and a stream. When the daemon has no session,
the entry has to keep being a channel - somebody in a living room has a remote
and nothing else - so the channel becomes this card: the code in letters big
enough to read, the QR to avoid typing it, and the address to type it at.

Why draw it in Python instead of letting ffmpeg burn in text: fonts are not
where you left them. Every OS ships different ones, a minimal player box may
have none, and escaping text for a filter graph is a bug waiting for a
quotation mark. A 5x7 bitmap table for the characters a code and a URL use is
less code, and it looks the same on every machine that runs this.

The layout is fixed rather than responsive: 1280x720 is 16:9, cheap to encode
as a still image, and large enough that the QR survives H.264 and a TV at sofa
distance.
"""

from __future__ import annotations

from . import qr
from .png import Canvas

# 5 columns by 7 rows per glyph, '#' dark. Only what appears on the card:
# the alphabet of a pairing code, a URL without scheme, and its punctuation.
_FONT: dict[str, tuple[str, ...]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    "/": ("....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."),
    ":": (".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."),
}

_PALE = 235   # body text: white enough to read, dim enough not to glare
_DIM = 140    # labels
_INK = 14     # background, near black
_CLEAR = 255  # the QR's own white tile


def _advance(scale: int) -> int:
    """One character cell: five columns plus a space between glyphs."""
    return 6 * scale


def width(text: str, scale: int) -> int:
    """The pixel width `_draw` will use. The trailing gap is not drawn."""
    return max(0, len(text) * _advance(scale) - scale)


def _draw(canvas: Canvas, text: str, x: int, y: int, scale: int,
          shade: int = _PALE) -> None:
    for index, char in enumerate(text.upper()):
        glyph = _FONT.get(char)
        if glyph is None:
            # An unknown character is a gap, not a crash: a URL with a rare
            # character still shows the rest of itself.
            continue
        left = x + index * _advance(scale)
        for row_index, row in enumerate(glyph):
            for col_index, cell in enumerate(row):
                if cell == "#":
                    canvas.fill(
                        left + col_index * scale, y + row_index * scale,
                        scale, scale, shade,
                    )


def _draw_centred(canvas: Canvas, text: str, y: int, scale: int,
                  shade: int = _PALE) -> None:
    _draw(canvas, text, (canvas.width - width(text, scale)) // 2, y, scale, shade)


def _fit(text: str, scale: int, room: int) -> int:
    """Shrink `scale` until `text` fits in `room` pixels, never below 1."""
    while scale > 1 and width(text, scale) > room:
        scale -= 1
    return scale


def display_page(page: str) -> str:
    """`https://id.nlziet.nl/device` -> `ID.NLZIET.NL/DEVICE`.

    The scheme is the same for every deployment and a person typing it only
    needs the host: a card that says HTTPS:// as well wastes the widest line.
    """
    return page.split("://", 1)[-1].rstrip("/").upper()


def pairing_card(
    url: str,
    code: str,
    page: str,
    width: int = 1280,
    height: int = 720,
) -> bytes:
    """The card as a PNG. `url` goes in the QR, `page` is what a human types."""
    grid = qr.matrix(url)
    # 420 px of QR is about a third of the frame: big enough for a phone across
    # a room, small enough to leave a column for the code at a readable size.
    scale = max(2, min(14, 420 // (len(grid) + 8)))
    tile = (len(grid) + 8) * scale

    canvas = Canvas(width, height, _INK)
    _draw_centred(canvas, "KOPPEL DIT APPARAAT", 52, 5, _PALE)

    # The symbol sits on its own white tile, with the four-module quiet zone
    # the encoder leaves around the modules: on a dark card the tile *is* the
    # margin, and a scanner needs that margin to find the symbol at all.
    qr_left = 88
    qr_top = 140 + max(0, (height - 200 - tile) // 2)
    canvas.fill(qr_left, qr_top, tile, tile, _CLEAR)
    canvas.stamp(grid, qr_left + 4 * scale, qr_top + 4 * scale, scale)

    column = qr_left + tile + 76
    room = width - column - 72
    label = _fit("SCAN DE QR-CODE", 4, room)
    _draw(canvas, "SCAN DE QR-CODE", column, qr_top + 22, label, _DIM)

    # The code is the point of the card, so it gets the largest type that fits.
    code_scale = _fit(code, 14, room)
    _draw(canvas, code, column, qr_top + 74, code_scale, _PALE)

    _draw(canvas, "OF OPEN", column, qr_top + 214, _fit("OF OPEN", 4, room), _DIM)
    target = display_page(page)
    _draw(canvas, target, column, qr_top + 262, _fit(target, 5, room), _PALE)

    hint = _fit("EN VUL DE CODE IN", 4, room)
    _draw(canvas, "EN VUL DE CODE IN", column, qr_top + 340, hint, _DIM)

    return canvas.png()
