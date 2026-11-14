"""A QR encoder: byte mode, error correction L or M, versions 1 to 10 (up to 213 bytes at M, 271 at L). No dependency.

The evidence QR of the harness box (view.html) holds where a view reads from; an AI that reads the screen decodes it. A text that
is longer than one code holds is chained: chain() cuts it into numbered parts, one code each. The algorithm follows the
ISO/IEC 18004 text; the tests decode every version and level with OpenCV's reader."""
from __future__ import annotations

import re

# Per version 0..10 (index 0 is unused): error correction codewords per block, and number of blocks.
ECC_PER_BLOCK = {"L": (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18), "M": (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)}
BLOCKS = {"L": (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4), "M": (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)}
FORMAT_BITS = {"L": 1, "M": 0}
MAX_VERSION = 10


def raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def data_codewords(ver, ecl):
    return raw_modules(ver) // 8 - ECC_PER_BLOCK[ecl][ver] * BLOCKS[ecl][ver]


def capacity(ecl="M"):
    """The most bytes one code holds (version 10)."""
    return (data_codewords(MAX_VERSION, ecl) * 8 - 4 - 16) // 8


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _bits(value, n):
    return [(value >> i) & 1 for i in range(n - 1, -1, -1)]


def _alignment(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    return [6] + list(range(size - 7 - step * (n - 2), size - 6, step))


class _Grid:
    def __init__(self, ver):
        self.ver, self.size = ver, ver * 4 + 17
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set(self, x, y, dark, fn=True):
        self.m[y][x] = bool(dark)
        if fn:
            self.fn[y][x] = True

    def finder(self, cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.size and 0 <= y < self.size:
                    d = max(abs(dx), abs(dy))
                    self.set(x, y, d not in (2, 4))

    def align(self, cx, cy):
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def function_patterns(self):
        for i in range(self.size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        self.finder(3, 3)
        self.finder(self.size - 4, 3)
        self.finder(3, self.size - 4)
        pos = _alignment(self.ver)
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                if not ((i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0)):
                    self.align(cx, cy)
        self.format(0, 0)  # reserve
        self.version_info()

    def format(self, ecl_bits, mask):
        data = ecl_bits << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        b = lambda i: (bits >> i) & 1
        for i in range(0, 6):
            self.set(8, i, b(i))
        self.set(8, 7, b(6))
        self.set(8, 8, b(7))
        self.set(7, 8, b(8))
        for i in range(9, 15):
            self.set(14 - i, 8, b(i))
        for i in range(0, 8):
            self.set(self.size - 1 - i, 8, b(i))
        for i in range(8, 15):
            self.set(8, self.size - 15 + i, b(i))
        self.set(8, self.size - 8, True)

    def version_info(self):
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.ver << 12 | rem
        for i in range(18):
            bit = (bits >> i) & 1
            a, b = self.size - 11 + i % 3, i // 3
            self.set(a, b, bit)
            self.set(b, a, bit)

    def place(self, codewords):
        bits = [b for c in codewords for b in _bits(c, 8)]
        i = 0
        for right in range(self.size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    up = ((right + 1) & 2) == 0
                    y = self.size - 1 - vert if up else vert
                    if not self.fn[y][x] and i < len(bits):
                        self.m[y][x] = bool(bits[i])
                        i += 1

    def mask(self, k):
        for y in range(self.size):
            for x in range(self.size):
                inv = [(x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0, (x // 3 + y // 2) % 2 == 0,
                       x * y % 2 + x * y % 3 == 0, (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0][k]
                if inv and not self.fn[y][x]:
                    self.m[y][x] = not self.m[y][x]

    def penalty(self):
        n, m, total = self.size, self.m, 0
        for lines in (m, [list(c) for c in zip(*m)]):
            for row in lines:
                run = 1
                for i in range(1, n):
                    if row[i] == row[i - 1]:
                        run += 1
                        if run == 5:
                            total += 3
                        elif run > 5:
                            total += 1
                    else:
                        run = 1
                s = "".join("1" if v else "0" for v in row)
                total += 40 * len(re.findall("(?=10111010000|00001011101)", s))  # finder-like runs, overlapping ones too
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    total += 3
        dark = sum(v for row in m for v in row)
        cells = n * n
        total += 10 * ((abs(dark * 20 - cells * 10) + cells - 1) // cells - 1)  # how far the share of dark modules is from half
        return total


def _codewords(data: bytes, ver, ecl):
    cap = data_codewords(ver, ecl)
    bits = _bits(0b0100, 4) + _bits(len(data), 8 if ver < 10 else 16)
    for b in data:
        bits += _bits(b, 8)
    bits += [0] * min(4, cap * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < cap:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    nblocks, ecc_len = BLOCKS[ecl][ver], ECC_PER_BLOCK[ecl][ver]
    raw = raw_modules(ver) // 8
    short = nblocks - raw % nblocks
    short_len = raw // nblocks
    div = _divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nblocks):
        n = short_len - ecc_len + (0 if i < short else 1)
        d = words[k:k + n]
        k += n
        blocks.append((d, _remainder(d, div)))
    out = []
    for i in range(len(blocks[-1][0])):
        for j, (d, _) in enumerate(blocks):
            if i != short_len - ecc_len or j >= short:
                if i < len(d):
                    out.append(d[i])
    for i in range(ecc_len):
        for _, e in blocks:
            out.append(e[i])
    return out


def encode(data, ecl="M", mask=None):
    """The modules of the QR for `data` (bytes or str): a list of rows of booleans, True for dark. No quiet zone.
    Raises ValueError when it does not fit version 10 (use chain()). mask: force one of the 8 masks (the tests do);
    None picks the one with the lowest penalty."""
    data = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    for ver in range(1, MAX_VERSION + 1):
        if len(data) * 8 + 4 + (8 if ver < 10 else 16) <= data_codewords(ver, ecl) * 8:
            break
    else:
        raise ValueError("%d bytes do not fit one code (at most %d)" % (len(data), capacity(ecl)))
    words = _codewords(data, ver, ecl)
    best = None
    for k in ([mask] if mask is not None else range(8)):
        g = _Grid(ver)
        g.function_patterns()
        g.place(words)
        g.mask(k)
        g.format(FORMAT_BITS[ecl], k)
        p = g.penalty()
        if best is None or p < best[0]:
            best = (p, g)
    assert best is not None
    return best[1].m


def chain(text, ecl="M", limit=None):
    """Cut `text` into parts that each fit one code (at most `limit` bytes of payload). A text that fits is one part with no
    header; a longer one gets "i/n|" before each part, so a reader puts them back in order."""
    data = text.encode("utf-8")
    limit = limit or capacity(ecl)
    if len(data) <= limit:
        return [text]
    size = limit - 8  # room for the header "ii/nn|"
    cuts, i = [], 0
    while i < len(data):
        j = min(len(data), i + size)
        while j < len(data) and (data[j] & 0xC0) == 0x80:  # never inside a UTF-8 character
            j -= 1
        cuts.append(data[i:j].decode("utf-8"))
        i = j
    return ["%d/%d|%s" % (n + 1, len(cuts), c) for n, c in enumerate(cuts)]


def rows(modules):
    """A matrix as strings of 0 and 1 (what the route sends)."""
    return ["".join("1" if v else "0" for v in row) for row in modules]
