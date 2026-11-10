#!/usr/bin/env python3
"""Generate the HotNews Daily logo: gradient badge with a white "7" + wordmark.

Brand mark is the numeral 7 (the site is little7.net), not a lightning bolt.
Run from anywhere:  python3 tools/make_logo.py
Outputs into site/static/images/.

Wordmark proportions: the badge and the text are sized to roughly equal
heights (BADGE_RATIO and TEXT_RATIO are fractions of the canvas height), so
neither dominates the other.
"""
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Brand colors
C_TOP = (255, 122, 24)      # orange
C_BOT = (217, 18, 40)       # red
C_DARK = (20, 33, 61)       # navy
C_RED = (217, 18, 40)

# Wordmark proportions (fractions of canvas height)
BADGE_RATIO = 0.93
TEXT_RATIO = 0.72


def make_badge(size=400, radius_ratio=0.24):
    """Rounded-square badge: orange->red vertical gradient with a white '7'."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / (size - 1)
        r = int(C_TOP[0] + (C_BOT[0] - C_TOP[0]) * t)
        g = int(C_TOP[1] + (C_BOT[1] - C_TOP[1]) * t)
        b = int(C_TOP[2] + (C_BOT[2] - C_TOP[2]) * t)
        gd.line([(0, y), (size, y)], fill=(r, g, b, 255))
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * radius_ratio), fill=255)
    img.paste(grad, (0, 0), mask)

    # white "7", optically centred (nudge down: the glyph has no descender)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_BOLD, int(size * 0.68))
    bbox = d.textbbox((0, 0), "7", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1] + int(size * 0.02)
    d.text((x, y), "7", font=font, fill=(255, 255, 255, 255))
    return img


def make_wordmark(h=150, c1=C_DARK, c2=C_RED):
    """Full logo: badge + 'HotNews Daily' wordmark (canvas auto-sized to text)."""
    badge_size = int(h * BADGE_RATIO)
    badge = make_badge(badge_size)
    pad_x = int(h * 0.12)
    pad_right = int(h * 0.06)

    tmp = Image.new("RGBA", (10, 10))
    td = ImageDraw.Draw(tmp)
    # For "HotNews Daily" in DejaVu Sans Bold the glyph bbox height is ~0.976*F.
    font = ImageFont.truetype(FONT_BOLD, int(h * TEXT_RATIO / 0.976))
    t1, t2 = "HotNews", " Daily"
    w1 = td.textlength(t1, font=font)
    w2 = td.textlength(t2, font=font)
    text_w = w1 + w2

    W = int(badge_size + pad_x + text_w + pad_right)
    H = h
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # badge vertically centred in the canvas
    img.paste(badge, (0, (H - badge_size) // 2), badge)

    d = ImageDraw.Draw(img)
    x = badge_size + pad_x
    b1 = d.textbbox((0, 0), t1, font=font)
    text_h = b1[3] - b1[1]
    y = (H - text_h) // 2 - b1[1]
    d.text((x, y), t1, font=font, fill=c1 + (255,))
    d.text((x + w1, y), t2, font=font, fill=c2 + (255,))
    return img


def main():
    import os
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "site", "static", "images")
    os.makedirs(out, exist_ok=True)
    # full logo (dark wordmark) — light backgrounds / JSON-LD / RSS
    wm = make_wordmark(150)
    wm.save(f"{out}/logo.png")
    print(f"logo.png: {wm.size}")
    # light variant (white wordmark) — the dark site header
    wm_light = make_wordmark(150, c1=(255, 255, 255), c2=(255, 193, 66))
    wm_light.save(f"{out}/logo-light.png")
    print(f"logo-light.png: {wm_light.size}")
    # square badge only -> favicons
    badge = make_badge(180)
    badge.save(f"{out}/favicon-180.png")
    badge.resize((64, 64), Image.Resampling.LANCZOS).save(f"{out}/favicon-64.png")
    badge.resize((32, 32), Image.Resampling.LANCZOS).save(f"{out}/favicon-32.png")
    badge.save(f"{out}/apple-touch-icon.png")
    # multi-size favicon.ico
    ico = make_badge(256)
    ico.save(f"{out}/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print("favicons: ico + 180/64/32 + apple-touch-icon")


if __name__ == "__main__":
    main()
