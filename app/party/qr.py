"""QR codes, for the host panel's "Watch on your phone": a phone's camera opens
the link it holds, and nobody types a 32-character token on a phone keyboard.

Byte mode, error correction level M (15 % of the code can be lost), versions 1
to 10: up to 213 bytes, and the link is about 64 (version 5, 37 x 37 modules).
Nothing else is needed from the standard, so nothing else is here. The mask is
chosen by the standard's penalty rules. Written from ISO/IEC 18004; checked by
tests/test_qr (Reed-Solomon against the standard's worked example, the format
and version bits against their published tables, and a decoder of its own
reading back every version and mask).

    matrix = qr.encode("http://192.168.1.251:42170/p/…/")    # rows of bools, True dark
"""

from __future__ import annotations

# version -> (EC codewords per block, [(blocks, data codewords per block), ...]), level M
_BLOCKS = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}
_LEVEL_M = 0b00                 # the two format bits for level M
MAX_VERSION = max(_BLOCKS)


class QRError(ValueError):
    """Too much to fit in a code this module makes."""


# --- Reed-Solomon over GF(256), x^8 + x^4 + x^3 + x^2 + 1 ----------------------

def _gf_multiply(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree: int) -> list[int]:
    """The generator polynomial (x - a^0)(x - a^1)...(x - a^(degree-1)), leading 1 dropped."""
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_multiply(root, 0x02)
    return result


def rs_remainder(data: list[int], degree: int) -> list[int]:
    """The error correction codewords for one block of data codewords."""
    divisor = _rs_divisor(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result[0]
        result = result[1:] + [0]
        for i in range(degree):
            result[i] ^= _gf_multiply(divisor[i], factor)
    return result


# --- the bits ------------------------------------------------------------------------

def _data_capacity(version: int) -> int:
    _, groups = _BLOCKS[version]
    return sum(count * size for count, size in groups)


def version_for(length: int) -> int:
    """The smallest version that holds `length` bytes, or QRError."""
    for version in _BLOCKS:
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * length <= _data_capacity(version) * 8:
            return version
    raise QRError(f"{length} bytes is more than a version {MAX_VERSION} code holds")


def _codewords(data: bytes, version: int) -> list[int]:
    """Mode, count, the bytes, the terminator and padding, then error correction,
    the blocks interleaved as the standard lays them out."""
    capacity = _data_capacity(version)
    count_bits = 8 if version < 10 else 16
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> i) & 1 for i in range(width - 1, -1, -1))

    put(0b0100, 4)                          # byte mode
    put(len(data), count_bits)
    for byte in data:
        put(byte, 8)
    put(0, min(4, capacity * 8 - len(bits)))    # terminator
    put(0, (-len(bits)) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11

    ec_len, groups = _BLOCKS[version]
    blocks: list[list[int]] = []
    at = 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[at:at + size])
            at += size
    ec_blocks = [rs_remainder(block, ec_len) for block in blocks]
    out: list[int] = []
    for i in range(max(len(block) for block in blocks)):
        out.extend(block[i] for block in blocks if i < len(block))
    for i in range(ec_len):
        out.extend(block[i] for block in ec_blocks)
    return out


def format_bits(mask: int, level: int = _LEVEL_M) -> int:
    """The 15 format bits: level and mask, BCH-protected, XORed with 101010000010010."""
    data = (level << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def version_bits(version: int) -> int:
    """The 18 version bits of a version 7 and up."""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _Grid:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def function_patterns(self) -> None:
        size = self.size
        for i in range(size):                               # timing
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):     # finders and separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        centres = _ALIGNMENT[self.version]
        last = len(centres) - 1
        for i, cx in enumerate(centres):
            for j, cy in enumerate(centres):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue                                # a finder is there
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format(0)                                      # reserved; the real one comes last
        if self.version >= 7:
            bits = version_bits(self.version)
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self.set(a, b, dark)
                self.set(b, a, dark)

    def format(self, mask: int) -> None:
        bits = format_bits(mask)
        size = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, size - 15 + i, bit(i))
        self.set(8, size - 8, True)                         # always dark

    def place(self, words: list[int]) -> None:
        """The codewords, two columns at a time from the bottom right, up then down,
        skipping the vertical timing pattern. Remainder bits stay light."""
        size = self.size
        total = len(words) * 8
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(size):
                y = size - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        test = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]


def _penalty(dark: list[list[bool]]) -> int:
    """The standard's four rules: runs of five or more, 2x2 blocks, finder-like
    runs, and the balance of dark and light."""
    size = len(dark)
    score = 0
    lines = dark + [[dark[y][x] for y in range(size)] for x in range(size)]
    finder = (True, False, True, True, True, False, True)
    for line in lines:
        run, colour = 0, None
        for value in line:
            if value == colour:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run, colour = 1, value
        if run >= 5:
            score += 3 + run - 5
        for i in range(size - 6):
            if tuple(line[i:i + 7]) == finder:
                before = line[max(0, i - 4):i]
                after = line[i + 7:i + 11]
                if (i >= 4 and not any(before)) or (i + 11 <= size and not any(after)):
                    score += 40
    for y in range(size - 1):
        for x in range(size - 1):
            if dark[y][x] == dark[y][x + 1] == dark[y + 1][x] == dark[y + 1][x + 1]:
                score += 3
    total = size * size
    darks = sum(map(sum, dark))
    score += 10 * (abs(darks * 20 - total * 10) // total)
    return score


def encode(text: str | bytes, mask: int | None = None) -> list[list[bool]]:
    """The code for `text` (UTF-8), as rows of modules, True for dark, without
    the quiet zone (leave 4 light modules around it). The smallest version that
    fits; the mask with the lowest penalty unless one is asked for."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = version_for(len(data))
    words = _codewords(data, version)
    best: tuple[int, list[list[bool]]] | None = None
    for candidate in range(8) if mask is None else (mask,):
        grid = _Grid(version)
        grid.function_patterns()
        grid.place(words)
        grid.apply_mask(candidate)
        grid.format(candidate)
        score = _penalty(grid.dark) if mask is None else 0
        if best is None or score < best[0]:
            best = (score, grid.dark)
    return best[1]
