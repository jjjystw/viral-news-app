#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""앱 아이콘 생성기 — icon.svg / icon-192.png / icon-512.png / apple-touch-icon.png

    pip install pillow && python3 make_icons.py

앱 테마(배경 #08090c 다크, 포인트 그라데이션 #ff4d3d→#ffcf5a)에 맞춘
다크 사각 아이콘 위에 오렌지 그라데이션 번개(속보) 마크를 얹는다.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

APP = Path(__file__).resolve().parent
SS = 4  # 슈퍼샘플링 배율


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(lerp(c1[i], c2[i], t))) for i in range(3))


def bolt_points(size: int) -> list[tuple[float, float]]:
    """가운데 번개. 0~1 정규화 좌표 → 캔버스 좌표."""
    raw = [
        (0.585, 0.115), (0.295, 0.575), (0.462, 0.575),
        (0.400, 0.885), (0.710, 0.455), (0.525, 0.455),
    ]
    return [(x * size, y * size) for x, y in raw]


def dark_backdrop(size: int) -> Image.Image:
    """#08090c 바탕 + 좌상단 오렌지, 우하단 블루 톤의 은은한 광."""
    img = Image.new("RGB", (size, size), (8, 9, 12))
    px = img.load()
    for y in range(size):
        for x in range(size):
            nx, ny = x / size, y / size
            d1 = ((nx - 0.18) ** 2 + (ny - 0.10) ** 2) ** 0.5
            d2 = ((nx - 0.95) ** 2 + (ny - 0.98) ** 2) ** 0.5
            r, g, b = 8, 9, 12
            k1 = max(0.0, 1.0 - d1 * 1.35)
            k2 = max(0.0, 1.0 - d2 * 1.5)
            r += int(58 * k1 ** 2)
            g += int(20 * k1 ** 2)
            b += int(6 * k1 ** 2)
            r += int(10 * k2 ** 2)
            g += int(24 * k2 ** 2)
            b += int(58 * k2 ** 2)
            px[x, y] = (min(255, r), min(255, g), min(255, b))
    return img


def orange_gradient(size: int) -> Image.Image:
    """번개에 채울 그라데이션: 위는 진한 빨강, 아래는 노랑."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    top, mid, bot = (255, 77, 61), (255, 138, 61), (255, 207, 90)
    for y in range(size):
        t = y / size
        c = mix(top, mid, t / 0.55) if t < 0.55 else mix(mid, bot, (t - 0.55) / 0.45)
        for x in range(size):
            px[x, y] = c
    return img


def render(size: int, rounded: bool) -> Image.Image:
    s = size * SS
    bg = dark_backdrop(s).convert("RGBA")

    # 번개 뒤 오렌지 후광
    halo = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(halo).polygon(bolt_points(s), fill=(255, 110, 60, 120))
    halo = halo.filter(ImageFilter.GaussianBlur(s * 0.075))
    halo.putalpha(halo.getchannel("A").point(lambda v: int(v * 0.85)))
    bg = Image.alpha_composite(bg, halo)

    # 번개 몸통 = 그라데이션
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).polygon(bolt_points(s), fill=255)
    bolt = orange_gradient(s).convert("RGBA")
    bolt.putalpha(mask)
    bg = Image.alpha_composite(bg, bolt)

    # 살짝 밝은 테두리로 또렷하게
    edge = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(edge).line(bolt_points(s) + [bolt_points(s)[0]],
                              fill=(255, 232, 190, 90), width=max(1, s // 220), joint="curve")
    bg = Image.alpha_composite(bg, edge)

    if rounded:
        m = Image.new("L", (s, s), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.235), fill=255)
        bg.putalpha(m)

    return bg.resize((size, size), Image.LANCZOS)


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" role="img" aria-label="바이럴뉴스">
  <defs>
    <linearGradient id="bolt" x1="0.2" y1="0" x2="0.85" y2="1">
      <stop offset="0" stop-color="#ff4d3d"/>
      <stop offset="0.55" stop-color="#ff8a3d"/>
      <stop offset="1" stop-color="#ffcf5a"/>
    </linearGradient>
    <radialGradient id="glowA" cx="0.18" cy="0.10" r="0.75">
      <stop offset="0" stop-color="#ff5a3d" stop-opacity="0.30"/>
      <stop offset="1" stop-color="#ff5a3d" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="glowB" cx="0.95" cy="0.98" r="0.6">
      <stop offset="0" stop-color="#74a8ff" stop-opacity="0.22"/>
      <stop offset="1" stop-color="#74a8ff" stop-opacity="0"/>
    </radialGradient>
    <filter id="blur" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="16"/>
    </filter>
  </defs>
  <rect width="512" height="512" rx="120" ry="120" fill="#08090c"/>
  <rect width="512" height="512" rx="120" ry="120" fill="url(#glowA)"/>
  <rect width="512" height="512" rx="120" ry="120" fill="url(#glowB)"/>
  <path filter="url(#blur)" fill="#ff6e3c" fill-opacity="0.55" d="M299 59 L151 294 h85 l-32 159 157-214 h-97 L299 59 Z"/>
  <path fill="url(#bolt)" d="M299 59 L151 294 h85 l-32 159 157-214 h-97 L299 59 Z"/>
</svg>
"""


def main() -> None:
    (APP / "icon.svg").write_text(SVG, encoding="utf-8")

    render(512, rounded=False).save(APP / "icon-512.png")          # any / maskable
    render(192, rounded=False).save(APP / "icon-192.png")
    render(180, rounded=False).save(APP / "apple-touch-icon.png")   # iOS가 모서리를 깎는다
    render(512, rounded=True).save(APP / "icon-512-rounded.png")    # 브라우저 탭·미리보기용
    print("아이콘 생성 완료:", APP)


if __name__ == "__main__":
    main()
