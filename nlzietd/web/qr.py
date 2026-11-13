"""A QR code, as a PNG, with no dependencies.

Why this exists at all: the pairing panel can draw a QR with a JavaScript
library from a CDN, but an M3U player cannot. Its only drawing surface for a
channel is `tvg-logo`, so if a playlist is going to show a scannable code, the
daemon has to serve an image. Same image for the page and the logo means one
implementation, one thing to get right, and no third party sees a pairing code.

What is deliberately not implemented: versions 7-40, numeric/alphanumeric/kanji
mode, and structured append. A pairing URL is 44 bytes, which fits version 4 at
error-correction level M; the table stops at version 6, which leaves room for a
longer base URL and fails loudly beyond that instead of emitting a wrong code.

The mask is chosen with three of the four scoring rules. The fourth looks for
finder-like patterns, which exist to protect a printed symbol from damage; on a
clean screen render it changes nothing, and leaving it out keeps this file
readable. Any mask decodes: the format bits say which one was used, so the
choice is about robustness, not correctness.
"""

from __future__ import annotations

from .png import modules as png_modules

# (blocks, data codewords per block, error-correction codewords per block) for
# level M. Level M survives ~15% loss, the usual default for a screen.
_BLOCKS = {
    1: (1, 16, 10),
    2: (1, 28, 16),
    3: (1, 44, 26),
    4: (2, 32, 18),
    5: (2, 43, 24),
    6: (4, 27, 16),
}

# Alignment pattern centres for versions 2-6: one pattern, bottom right.
_ALIGNMENT = {2: 18, 3: 22, 4: 26, 5: 30, 6: 34}

# Format bits are two bits of level plus three of mask; M is 00.
_LEVEL_M = 0b00


def _mul(x: int, y: int) -> int:
    """Multiplication in GF(256) modulo x^8+x^4+x^3+x^2+1 (the QR field)."""
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(degree: int) -> list[int]:
    """The generator polynomial for `degree` error-correction codewords."""
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for i in range(len(result)):
            result[i] = _mul(result[i], root)
            if i + 1 < len(result):
                result[i] ^= result[i + 1]
        root = _mul(root, 2)
    return result


def _remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _mul(coefficient, factor)
    return result


def _capacity(version: int) -> int:
    blocks, data, _ecc = _BLOCKS[version]
    return blocks * data


def _payload(text: str) -> tuple[int, list[int]]:
    """Byte-mode data codewords, and the smallest version that holds them."""
    raw = text.encode("utf-8")
    for version in sorted(_BLOCKS):
        if len(raw) <= _capacity(version) - 2:
            break
    else:
        raise ValueError(
            f"{len(raw)} bytes is too long for a version-6 code at level M"
        )

    bits: list[int] = []

    def put(value: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)  # byte mode
    put(len(raw), 8)  # versions 1-9 use an 8-bit count
    for byte in raw:
        put(byte, 8)

    total = _capacity(version) * 8
    # Terminator, then pad to a byte boundary, then the two alternating pad
    # codewords the spec prescribes (0xEC/0x11).
    for _ in range(min(4, total - len(bits))):
        bits.append(0)
    while len(bits) % 8:
        bits.append(0)
    codewords = [
        int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)
    ]
    filled = len(codewords)
    pad = (0xEC, 0x11)
    while len(codewords) < _capacity(version):
        codewords.append(pad[(len(codewords) - filled) % 2])
    return version, codewords


def _interleave(version: int, codewords: list[int]) -> list[int]:
    """Split into blocks, add error correction, then interleave.

    Interleaving is what makes a scratch across the symbol spread its damage
    over several blocks, which is the whole point of blocks.
    """
    blocks, per_block, ecc_len = _BLOCKS[version]
    divisor = _divisor(ecc_len)
    data_blocks = [
        codewords[i * per_block:(i + 1) * per_block] for i in range(blocks)
    ]
    ecc_blocks = [_remainder(block, divisor) for block in data_blocks]

    out: list[int] = []
    for i in range(per_block):
        for block in data_blocks:
            out.append(block[i])
    for i in range(ecc_len):
        for block in ecc_blocks:
            out.append(block[i])
    return out


class _Grid:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        # Function modules are drawn before the data and must never be
        # overwritten by it or masked.
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, row: int, col: int, dark: bool) -> None:
        self.dark[row][col] = dark
        self.fixed[row][col] = True

    # -- function patterns ---------------------------------------------------

    def finder(self, top: int, left: int) -> None:
        # 7x7 eye, plus its separator: an 8x8 area with the corner trimmed.
        for row in range(-1, 8):
            for col in range(-1, 8):
                r, c = top + row, left + col
                if not (0 <= r < self.size and 0 <= c < self.size):
                    continue
                inside = 0 <= row <= 6 and 0 <= col <= 6
                ring = row in (0, 6) or col in (0, 6)
                core = 2 <= row <= 4 and 2 <= col <= 4
                self.set(r, c, inside and (ring or core))

    def timing(self) -> None:
        for i in range(8, self.size - 8):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)

    def alignment(self) -> None:
        centre = _ALIGNMENT.get(self.version)
        if centre is None:
            return
        for row in range(-2, 3):
            for col in range(-2, 3):
                # Outer ring and centre dark, the ring between them light.
                self.set(centre + row, centre + col, max(abs(row), abs(col)) != 1)

    def reserve_format(self) -> None:
        """Mark the format areas as function modules without caring what they say.

        The real bits depend on the mask, which is not chosen until the data is
        in. Reserving first is what keeps the data out of those cells - and row
        and column 6 are skipped because they are the timing pattern, which
        happens to pass through this corner.
        """
        for i in range(9):
            if i == 6:
                continue  # timing module, not a format bit
            self.set(8, i, False)
            self.set(i, 8, False)
        for i in range(8):
            self.set(8, self.size - 1 - i, False)
            self.set(self.size - 1 - i, 8, False)
        self.set(8, self.size - 8, True)  # the always-dark module

    def draw_format(self, mask: int) -> None:
        data = (_LEVEL_M << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        bit = lambda i: bool((bits >> i) & 1)
        for i in range(6):
            self.set(i, 8, bit(i))
        self.set(7, 8, bit(6))
        self.set(8, 8, bit(7))
        self.set(8, 7, bit(8))
        for i in range(9, 15):
            self.set(8, 14 - i, bit(i))
        for i in range(8):
            self.set(8, self.size - 1 - i, bit(i))
        for i in range(8, 15):
            self.set(self.size - 15 + i, 8, bit(i))
        self.set(self.size - 8, 8, True)

    # -- data ----------------------------------------------------------------

    def place_data(self, codewords: list[int]) -> None:
        bits: list[int] = []
        for byte in codewords:
            for i in range(7, -1, -1):
                bits.append((byte >> i) & 1)

        index = 0
        right = self.size - 1
        upward = True
        while right >= 1:
            if right == 6:
                right = 5  # the vertical timing pattern is not a data column
            for step in range(self.size):
                row = self.size - 1 - step if upward else step
                for offset in range(2):
                    col = right - offset
                    if self.fixed[row][col] or index >= len(bits):
                        continue
                    self.dark[row][col] = bool(bits[index])
                    index += 1
            upward = not upward
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for row in range(self.size):
            for col in range(self.size):
                if self.fixed[row][col]:
                    continue
                self.dark[row][col] ^= _MASKS[mask](row, col)

    def penalty(self) -> int:
        score = 0
        size = self.size
        # Rule 1: runs of five or more in a line, 3 points plus 1 per extra.
        for line in self.rows() + self.columns():
            run = 1
            for i in range(1, size):
                if line[i] == line[i - 1]:
                    run += 1
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                else:
                    run = 1
        # Rule 2: every 2x2 block of one colour.
        for row in range(size - 1):
            for col in range(size - 1):
                block = {
                    self.dark[row][col],
                    self.dark[row][col + 1],
                    self.dark[row + 1][col],
                    self.dark[row + 1][col + 1],
                }
                if len(block) == 1:
                    score += 3
        # Rule 4: the further from half dark, the worse. Rule 3 (finder-like
        # patterns) is left out on purpose; see the module docstring.
        dark = sum(sum(1 for module in row if module) for row in self.dark)
        total = size * size
        # Push it towards 50%: a symbol that is mostly one colour is harder to
        # find and to sample.
        score += (abs(dark * 20 - total * 10) // total) * 10
        return score

    def rows(self) -> list[list[bool]]:
        return self.dark

    def columns(self) -> list[list[bool]]:
        return [list(col) for col in zip(*self.dark)]


_MASKS = {
    0: lambda r, c: (r + c) % 2 == 0,
    1: lambda r, c: r % 2 == 0,
    2: lambda r, c: c % 3 == 0,
    3: lambda r, c: (r + c) % 3 == 0,
    4: lambda r, c: (r // 2 + c // 3) % 2 == 0,
    5: lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    6: lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    7: lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
}


def matrix(text: str) -> list[list[bool]]:
    """The modules of a level-M code for `text`, dark = True."""
    version, codewords = _payload(text)
    grid = _Grid(version)
    grid.finder(0, 0)
    grid.finder(0, grid.size - 7)
    grid.finder(grid.size - 7, 0)
    grid.timing()
    grid.alignment()
    grid.reserve_format()
    grid.place_data(_interleave(version, codewords))

    # Try every mask on a copy; the format bits have to be redrawn for each
    # because they are not masked and they say which mask was used.
    best = None
    for mask in _MASKS:
        candidate = _Grid(version)
        candidate.dark = [row[:] for row in grid.dark]
        candidate.fixed = [row[:] for row in grid.fixed]
        candidate.apply_mask(mask)
        candidate.draw_format(mask)
        score = candidate.penalty()
        if best is None or score < best[0]:
            best = (score, candidate)
    assert best is not None
    return best[1].dark


def encode_png(modules: list[list[bool]], scale: int = 1, border: int = 0) -> bytes:
    """A symbol as a PNG: dark on light, `border` quiet modules around it.

    Dark on light because a scanner looks for the light margin around the
    symbol; white-on-black has no quiet zone and confuses more readers than it
    helps. Shared with the pairing card, which pastes this same image into a
    video frame - one encoder, so the page, the playlist logo and the stream
    cannot disagree about the code.
    """
    return png_modules(modules, scale, border)


def png(text: str, scale: int = 8, border: int = 4) -> bytes:
    """The whole image, ready to serve as `image/png`."""
    return encode_png(matrix(text), scale, border)
