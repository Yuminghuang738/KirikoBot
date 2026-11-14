#!/usr/bin/env python3
"""生成项目图标 assets/icon.png（512×512，樱花粉 + K）。

规格见 docs/icon-spec.md；CI/运行不依赖本脚本，只在需要重生成时手动跑：
    python3 scripts/gen_icon.py
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFilter

SIZE = 512
MARGIN = 32
RADIUS = 96
STEM_X = 176
TOP_Y = 116
BOTTOM_Y = 396
JOINT_Y = 258
ARM_TOP = (312, 126)
ARM_BOTTOM = (332, 392)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def build_background() -> Image.Image:
    top_left = (0xFF, 0xD9, 0xE8)
    bottom_right = (0xFF, 0x9E, 0xC4)
    img = Image.new("RGBA", (SIZE, SIZE))
    px = img.load()
    for y in range(SIZE):
        for x in range(SIZE):
            t = (x + y) / (2 * (SIZE - 1))
            r, g, b = _lerp(top_left, bottom_right, t)
            px[x, y] = (r, g, b, 255)

    # 左上柔和白色径向光：中心 (128,128)，半径 60%，最高 alpha 35
    glow = Image.new("RGBA", (SIZE, SIZE))
    gpx = glow.load()
    cx, cy = 128, 128
    radius = SIZE * 0.6
    for y in range(SIZE):
        for x in range(SIZE):
            d = math.hypot(x - cx, y - cy) / radius
            if d < 1:
                alpha = int(35 * (1 - d) ** 2)
                gpx[x, y] = (255, 255, 255, alpha)
    img = Image.alpha_composite(img, glow)

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN), RADIUS, fill=255,
    )
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out, mask


def _draw_k(layer: Image.Image, color: tuple[int, int, int, int], width: int) -> None:
    draw = ImageDraw.Draw(layer)
    r = width // 2
    points = [
        ((STEM_X, TOP_Y), (STEM_X, BOTTOM_Y)),
        ((STEM_X, JOINT_Y), ARM_TOP),
        ((STEM_X, JOINT_Y), ARM_BOTTOM),
    ]
    for start, end in points:
        draw.line([start, end], fill=color, width=width)
        for cx, cy in (start, end):
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)


def _sakura(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: float,
            petal: tuple[int, int, int, int], edge: tuple[int, int, int, int]) -> None:
    for i in range(5):
        angle = math.radians(-90 + i * 72)
        px = cx + math.cos(angle) * radius * 0.58
        py = cy + math.sin(angle) * radius * 0.58
        pr = radius * 0.46
        draw.ellipse((px - pr, py - pr * 1.15, px + pr, py + pr * 1.15), fill=petal, outline=edge, width=2)
    cr = radius * 0.2
    draw.ellipse((cx - cr, cy - cr, cx + cr, cy + cr), fill=(0xFF, 0xD1, 0xE3, 255))


def _petal(canvas: Image.Image, cx: int, cy: int, size: int, angle: float, alpha: int) -> None:
    layer = Image.new("RGBA", (size * 3, size * 3), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((size, size // 2, size * 2, size * 2), fill=(0xFF, 0xB7, 0xD5, alpha))
    rotated = layer.rotate(angle, resample=Image.BICUBIC, expand=False)
    canvas.alpha_composite(rotated, (cx - size * 3 // 2, cy - size * 3 // 2))


def build_icon() -> Image.Image:
    canvas, mask = build_background()

    # K 阴影：深玫瑰轮廓 + 模糊
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    _draw_k(shadow, (0x7A, 0x33, 0x52, 255), 68)
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    shadow.putalpha(shadow.getchannel("A").point(lambda a: int(a * 0.25)))
    canvas.alpha_composite(shadow, (0, 6))

    # K 外描边 + 白色主体
    k_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    _draw_k(k_layer, (0x7A, 0x33, 0x52, 255), 68)
    _draw_k(k_layer, (255, 255, 255, 255), 56)
    canvas.alpha_composite(k_layer)

    # 右上樱花 + 飘落花瓣
    flower = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    _sakura(ImageDraw.Draw(flower), 392, 118, 58,
            (0xFF, 0xF0, 0xF6, 255), (0xFF, 0xB7, 0xD5, 255))
    canvas.alpha_composite(flower)

    _petal(canvas, 96, 404, 11, 24, 140)
    _petal(canvas, 404, 330, 12, -32, 90)
    _petal(canvas, 352, 442, 9, 68, 64)

    # 统一裁回圆角，避免花瓣糊出画布
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(canvas, (0, 0), mask)
    return out


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "assets")
    os.makedirs(out_dir, exist_ok=True)
    icon = build_icon()
    path = os.path.join(out_dir, "icon.png")
    icon.save(path, "PNG")
    print(f"wrote {path} ({icon.size[0]}x{icon.size[1]})")


if __name__ == "__main__":
    main()
