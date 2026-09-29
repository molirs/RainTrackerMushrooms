"""Genera app/static/icono.ico (seta bajo la lluvia) sin dependencias externas.

    python -m scripts.make_icon
"""
import struct
import zlib
from pathlib import Path

SIZE = 64
CAP, DOT, STEM, DROP = (200, 45, 40, 255), (255, 255, 255, 255), (238, 222, 196, 255), (66, 146, 198, 255)


def pixel(x: float, y: float):
    # Gotas de lluvia
    for cx, cy in ((12, 10), (32, 5), (52, 11)):
        if (x - cx) ** 2 / 9 + (y - cy) ** 2 / 25 <= 1:
            return DROP
    # Sombrero: semielipse
    if y <= 38 and ((x - 32) / 28) ** 2 + ((y - 38) / 22) ** 2 <= 1:
        for cx, cy, r in ((22, 28, 4.5), (38, 24, 5), (48, 33, 3.5), (30, 35, 3)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                return DOT
        return CAP
    # Pie
    if 38 < y <= 60 and abs(x - 32) <= 9 - (y - 38) * 0.08 and not (y > 57 and abs(x - 32) > 7):
        return STEM
    return (0, 0, 0, 0)


def png_bytes() -> bytes:
    raw = b""
    for y in range(SIZE):
        raw += b"\x00"
        for x in range(SIZE):
            # Supermuestreo 3x3 para suavizar bordes
            acc = [0, 0, 0, 0]
            for dy in (0.17, 0.5, 0.83):
                for dx in (0.17, 0.5, 0.83):
                    p = pixel(x + dx, y + dy)
                    for i in range(3):
                        acc[i] += p[i] * p[3]
                    acc[3] += p[3]
            a = acc[3] / 9
            rgb = [int(acc[i] / acc[3]) if acc[3] else 0 for i in range(3)]
            raw += bytes(rgb + [int(a)])

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    png = png_bytes()
    ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32, len(png), 22) + png
    out = Path(__file__).resolve().parent.parent / "app" / "static" / "icono.ico"
    out.write_bytes(ico)
    print(out)


if __name__ == "__main__":
    main()
