"""Generate the marketing images: icon.png, featured.png, banner.png.

Run: python3 assets/make_assets.py   (needs Pillow)
Original artwork; no Autodesk or Blender logos are used.
"""
import math
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
BG0, BG1 = (13, 17, 26), (27, 34, 52)
TEAL, TEAL_D = (45, 212, 191), (20, 120, 112)
AMBER, AMBER_L, AMBER_D = (245, 158, 11), (253, 200, 90), (190, 105, 8)
WHITE, MUTED = (240, 244, 250), (150, 162, 184)
SS = 2  # supersampling


def font(size, bold=True):
    for path, idx in (("/System/Library/Fonts/SFNS.ttf", None),
                      ("/System/Library/Fonts/Helvetica.ttc", 1 if bold else 0),
                      ("C:/Windows/Fonts/segoeuib.ttf", None),
                      ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", None)):
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size, index=idx or 0)
                if path.endswith("SFNS.ttf"):
                    f.set_variation_by_name("Bold" if bold else "Regular")
                return f
            except Exception:
                continue
    return ImageFont.load_default()


def gradient(w, h):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = (x / w * 0.4 + y / h * 0.6)
            px[x, y] = tuple(int(BG0[i] + (BG1[i] - BG0[i]) * t) for i in range(3))
    return img


def cube(d, cx, cy, s, lw):
    """Isometric cube: wireframe (Maya side) on the left, solid (Blender) right."""
    k = s * 0.866
    P = lambda x, y: (cx + x, cy + y)
    top, ur, lr, bot = P(0, -s), P(k, -s / 2), P(k, s / 2), P(0, s)
    ll, ul, c = P(-k, s / 2), P(-k, -s / 2), P(0, 0)
    d.polygon([top, ur, c, ul], fill=AMBER_L)
    d.polygon([c, ur, lr, bot], fill=AMBER)
    d.polygon([ul, c, bot, ll], fill=(20, 30, 44))
    # wire grid on the left face
    n = 3
    lerp = lambda a, b, t: (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    for i in range(n + 1):
        t = i / n
        d.line([lerp(ul, c, t), lerp(ll, bot, t)], fill=TEAL, width=lw)
        d.line([lerp(ul, ll, t), lerp(c, bot, t)], fill=TEAL, width=lw)
    r = lw * 1.5
    for i in range(n + 1):
        for j in range(n + 1):
            a = (ul[0] + (c[0] - ul[0]) * i / n + (ll[0] - ul[0]) * j / n,
                 ul[1] + (c[1] - ul[1]) * i / n + (ll[1] - ul[1]) * j / n)
            d.ellipse([a[0] - r, a[1] - r, a[0] + r, a[1] + r], fill=WHITE)
    # edges
    for a, b in ((top, ur), (ur, lr), (lr, bot), (top, ul), (ul, ll), (ll, bot),
                 (c, top), (c, ur), (c, bot), (c, ul)):
        col = AMBER_D if (a in (ur, lr) or b in (ur, lr)) else TEAL_D
        d.line([a, b], fill=col, width=lw)
    d.line([ul, c], fill=TEAL, width=lw)
    d.line([c, bot], fill=TEAL, width=lw)


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius, fill=255)
    return m


def icon(path, px=256):
    big = px * SS * 2
    img = gradient(big, big)
    d = ImageDraw.Draw(img)
    cube(d, big / 2, big / 2 + big * 0.02, big * 0.33, max(2, big // 90))
    img = img.resize((px, px), Image.LANCZOS).convert("RGBA")
    img.putalpha(rounded_mask((px, px), px // 5))
    img.save(path)


def chip(d, xy, text, f, fill, fg):
    x, y = xy
    w = d.textlength(text, font=f)
    pad = f.size * 0.6
    h = f.size * 1.9
    d.rounded_rectangle([x, y, x + w + pad * 2, y + h], h / 2, fill=fill)
    d.text((x + pad, y + h / 2), text, font=f, fill=fg, anchor="lm")
    return x + w + pad * 2


def poster(path, W, H):
    u = H / 1080.0
    img = gradient(W * SS, H * SS)
    d = ImageDraw.Draw(img)
    S = lambda v: v * u * SS
    # soft glows
    glow = Image.new("RGB", img.size, (0, 0, 0))
    g = ImageDraw.Draw(glow)
    gx, gy = W * SS * 0.74, H * SS * 0.5
    g.ellipse([gx - S(380), gy - S(380), gx + S(380), gy + S(380)], fill=(20, 110, 100))
    glow = glow.filter(ImageFilter.GaussianBlur(S(180)))
    img = Image.blend(img, Image.composite(glow, img, Image.new("L", img.size, 255)), 0.0)
    from PIL import ImageChops
    img = ImageChops.add(img, glow)
    d = ImageDraw.Draw(img)
    left = W * 0.075 * SS
    d.text((left, S(300)), "Maya Scene IO", font=font(int(S(128))), fill=WHITE, anchor="ls")
    d.text((left, S(385)), "Import & export Maya scenes in Blender",
           font=font(int(S(52)), False), fill=MUTED, anchor="ls")
    f = font(int(S(34)))
    x = left
    for t in (".ma", ".mb"):
        x = chip(d, (x, S(440)), t, f, (20, 60, 62), TEAL) + S(16)
    d.text((x + S(4), S(440) + S(34) * 0.95), "\u2192", font=f, fill=MUTED, anchor="lm")
    chip(d, (x + S(56), S(440)), ".blend", f, (70, 45, 8), AMBER_L)
    fy = S(600)
    ff = font(int(S(38)), False)
    for i, t in enumerate(("Meshes, UVs and materials", "Skeleton, skin and blend shapes",
                           "Cameras, lights and animation")):
        yy = fy + i * S(66)
        d.ellipse([left, yy - S(9), left + S(18), yy + S(9)], fill=TEAL)
        d.text((left + S(40), yy), t, font=ff, fill=WHITE, anchor="lm")
    d.text((left, S(900)), "Windows  \u00b7  macOS  \u00b7  Linux     Blender 4.2+     GPL-3.0",
           font=font(int(S(30)), False), fill=MUTED, anchor="ls")
    d.text((left, S(950)), ".ma works with no Maya installed", font=font(int(S(30)), False),
           fill=TEAL, anchor="ls")
    cube(d, W * SS * 0.74, H * SS * 0.5, S(300), max(2, int(S(6))))
    img.resize((W, H), Image.LANCZOS).save(path)


if __name__ == "__main__":
    icon(os.path.join(HERE, "icon.png"))
    poster(os.path.join(HERE, "featured.png"), 1920, 1080)
    poster(os.path.join(HERE, "banner.png"), 1280, 640)
    print("done")
