"""Рисовалка пометок на скриншотах: рамки, стрелки, номерные кружки, подписи.
Координаты в спецификации — CSS px (как в <shot>.json); кадры сняты в 2x."""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 2  # device scale factor кадров
RED = (229, 72, 77)
WHITE = (255, 255, 255)
FONT_B = "/System/Library/Fonts/Helvetica.ttc"


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_B, size, index=1 if bold else 0)


class Fig:
    def __init__(self, shots: Path, name: str, crop: tuple | None = None, extend: tuple[int, int, int, int] = (0, 0, 0, 0)):
        """crop — (x0, y0, x1, y1) в CSS px; extend — белые поля (left, top, right, bottom) в CSS px."""
        self.img = Image.open(shots / f"{name}.png").convert("RGB")
        self.boxes = json.loads((shots / f"{name}.json").read_text()) if (shots / f"{name}.json").exists() else []
        self.ox = self.oy = 0  # смещение CSS-координат кадра относительно холста
        if crop:
            x0, y0, x1, y1 = (int(v * S) for v in crop)
            self.img = self.img.crop((x0, y0, min(x1, self.img.width), min(y1, self.img.height)))
            self.ox, self.oy = -crop[0], -crop[1]
        l, t, r, b = (int(v * S) for v in extend)
        if any((l, t, r, b)):
            canvas = Image.new("RGB", (self.img.width + l + r, self.img.height + t + b), WHITE)
            canvas.paste(self.img, (l, t)); self.img = canvas
            self.ox += extend[0]; self.oy += extend[1]
        self.d = ImageDraw.Draw(self.img)

    # --- поиск элементов по json кадра
    def find(self, text: str, sel: str | None = None, index: int = 0, exact: bool = False) -> tuple[float, float, float, float]:
        hits = [b for b in self.boxes if (sel is None or b["sel"] == sel) and ((b["text"] == text) if exact else (text in b["text"]))]
        if not hits:
            raise KeyError(f"нет элемента {text!r} ({sel})")
        b = hits[index]
        return (b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"])

    # --- перевод CSS-координат в пиксели холста
    def p(self, x: float, y: float) -> tuple[int, int]:
        return int((x + self.ox) * S), int((y + self.oy) * S)

    def rect(self, box, pad: float = 4):
        x0, y0 = self.p(box[0] - pad, box[1] - pad); x1, y1 = self.p(box[2] + pad, box[3] + pad)
        return x0, y0, x1, y1

    # --- примитивы
    def box(self, box, pad: float = 4, width: int = 5, radius: int = 10, color=RED):
        self.d.rounded_rectangle(self.rect(box, pad), radius=radius, outline=color, width=width)

    def underline(self, box, width: int = 5, color=RED):
        x0, y0 = self.p(box[0], box[3] + 2); x1, _ = self.p(box[2], box[3] + 2)
        self.d.line([(x0, y0), (x1, y0)], fill=color, width=width)

    def badge(self, n: int, at: tuple[float, float], r: int = 15, color=RED):
        cx, cy = self.p(*at); R = r * S
        self.d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=color, outline=WHITE, width=3)
        f = font(int(R * 1.15)); tw, th = self._tsize(str(n), f)
        self.d.text((cx - tw / 2, cy - th / 2 - R * 0.08), str(n), font=f, fill=WHITE)

    def _tsize(self, text, f):
        l, t, r, b = self.d.textbbox((0, 0), text, font=f); return r - l, b - t

    def label(self, text: str, at: tuple[float, float], anchor: str = "lt", size: int = 15, color=RED, n: int | None = None):
        """Подпись с белой подложкой; anchor — 'lt' (левый верх в at), 'rt', 'lb', 'rb', 'ct' (центр сверху), 'cb'.
        Возвращает прямоугольник подложки в px холста."""
        f = font(size * S)
        lines = text.split("\n"); pad = 8 * S
        widths = [self._tsize(l, f)[0] for l in lines]; lh = int(size * S * 1.3)
        w = max(widths) + 2 * pad + (0 if n is None else 36 * S); h = lh * len(lines) + 2 * pad
        x, y = self.p(*at)
        if anchor[0] == "r": x -= w
        elif anchor[0] == "c": x -= w // 2
        if anchor[1] == "b": y -= h
        self.d.rounded_rectangle((x, y, x + w, y + h), radius=8 * S, fill=WHITE, outline=color, width=3)
        tx = x + pad + (0 if n is None else 36 * S)
        if n is not None:
            self.badge(n, ((x + 18 * S) / S - self.ox, (y + h / 2) / S - self.oy), r=13)
        for i, line in enumerate(lines):
            self.d.text((tx, y + pad + i * lh), line, font=f, fill=color)
        return (x, y, x + w, y + h)

    def arrow(self, src: tuple[float, float], dst: tuple[float, float], width: int = 5, color=RED, head: int = 16):
        """Стрелка от src к dst (CSS px)."""
        x0, y0 = self.p(*src); x1, y1 = self.p(*dst)
        ang = math.atan2(y1 - y0, x1 - x0); H = head * S
        bx, by = x1 - H * math.cos(ang), y1 - H * math.sin(ang)  # основание наконечника
        self.d.line([(x0, y0), (bx, by)], fill=color, width=width)
        left = (bx + H * 0.5 * math.sin(ang), by - H * 0.5 * math.cos(ang))
        right = (bx - H * 0.5 * math.sin(ang), by + H * 0.5 * math.cos(ang))
        self.d.polygon([(x1, y1), left, right], fill=color)

    def callout(self, n: int, box, text: str, at: tuple[float, float], anchor: str = "lt", pad: float = 4, size: int = 15,
                arrow_to: str = "auto", style: str = "box"):
        """Рамка вокруг элемента + подпись с номером в at + стрелка от подписи к ближайшему краю рамки."""
        if style == "box":
            self.box(box, pad)
        elif style == "underline":
            self.underline(box)
        lb = self.label(text, at, anchor, size=size, n=n)
        # точки: центр ближайшей стороны подписи -> ближайшая точка рамки
        bx0, by0, bx1, by1 = self.rect(box, pad)
        lcx, lcy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        if arrow_to == "auto":
            arrow_to = ("left" if lcx < bx0 else "right" if lcx > bx1 else "top" if lcy < by0 else "bottom")
        dst = {"left": (bx0, min(max(lcy, by0), by1)), "right": (bx1, min(max(lcy, by0), by1)),
               "top": (min(max(lcx, bx0), bx1), by0), "bottom": (min(max(lcx, bx0), bx1), by1)}[arrow_to]
        src = {"left": (lb[2], lcy), "right": (lb[0], lcy), "top": (min(max(lcx, lb[0]), lb[2]), lb[3]), "bottom": (min(max(lcx, lb[0]), lb[2]), lb[1])}[arrow_to]
        if arrow_to in ("top", "bottom"): src = (lcx, src[1])
        if arrow_to in ("left", "right"): src = (src[0], lcy)
        self.arrow((src[0] / S - self.ox, src[1] / S - self.oy), (dst[0] / S - self.ox, dst[1] / S - self.oy))

    def save(self, path: Path, max_w: int = 1800, quality: int = 85):
        img = self.img
        if img.width > max_w:
            img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, quality=quality, optimize=True, progressive=True)
        print(path.name, img.size, f"{path.stat().st_size // 1024} KB")
