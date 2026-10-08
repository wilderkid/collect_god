# 生成扩展图标。纯标准库，画一块暖陶色底、一道白色书签。
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "extension", "icons")


def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png(size, pixels):
    raw = b"".join(b"\x00" + pixels[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(raw, 9)),
            chunk(b"IEND", b""),
        ]
    )


def draw(size):
    bg = (159, 61, 28, 255)
    fg = (255, 250, 245, 255)
    px = [bg] * (size * size)
    # 书签主体：居中的圆角矩形，底部中央一个 V 形缺口。
    left = round(size * 0.30)
    right = round(size * 0.70)
    top = round(size * 0.16)
    bottom = round(size * 0.84)
    notch_top = round(size * 0.62)
    mid = size / 2

    def inside_radius(x, y):
        radius = max(1, round(size * 0.06))
        for cx in (left + radius, right - 1 - radius):
            for cy in (top + radius, bottom - 1 - radius):
                if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 and (
                    (x < left + radius or x > right - 1 - radius)
                    and (y < top + radius or y > bottom - 1 - radius)
                ):
                    return True
        return left + radius <= x < right - radius or top + radius <= y < bottom - radius

    for y in range(size):
        for x in range(size):
            if not (left <= x < right and top <= y < bottom):
                continue
            if not inside_radius(x, y):
                continue
            # 缺口：从 notch_top 起向中线收拢。
            if y >= notch_top:
                span = (right - left) / 2
                progress = (y - notch_top) / max(1, (bottom - notch_top))
                if abs(x - mid) < progress * span * 0.72:
                    continue
            px[y * size + x] = fg
    return b"".join(bytes(c) for c in px)


def main():
    os.makedirs(OUT, exist_ok=True)
    for size in (16, 48, 128):
        path = os.path.join(OUT, "icon%d.png" % size)
        with open(path, "wb") as fh:
            fh.write(png(size, draw(size)))
        print(path)


if __name__ == "__main__":
    main()
