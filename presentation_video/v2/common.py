"""Drawing toolkit shared by every V2 scene: palette, fonts, sprites, easing."""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ASSETS = HERE / 'assets'
MANIFEST = json.loads((ASSETS / 'assets.json').read_text())

W, H, FPS = 1920, 1080, 60

INK = (20, 33, 58)
INK2 = (62, 76, 98)
MUTED = (118, 130, 148)
LINE = (222, 228, 234)
TEAL = (8, 134, 142)
TEAL_D = (4, 96, 104)
TEAL_L = (220, 241, 241)
GOLD = (226, 160, 46)
GOLD_L = (255, 243, 214)
RED = (220, 62, 56)
RED_L = (253, 228, 226)
AMBER = (236, 152, 28)
GREEN = (34, 160, 92)
GREEN_L = (222, 244, 231)
BLUE = (38, 92, 214)
NAVY = (9, 20, 38)
PURPLE = (88, 92, 196)
WHITE = (255, 255, 255)

AVENIR = '/System/Library/Fonts/Avenir Next.ttc'
MONO = '/System/Library/Fonts/SFNSMono.ttf'
WEIGHTS = {'bold': 0, 'demi': 2, 'medium': 5, 'regular': 7, 'heavy': 8, 'light': 10}


# ------------------------------------------------------------------ easing
def clamp(v, a=0.0, b=1.0):
    return a if v < a else b if v > b else v


def prog(t, a, b):
    return clamp((t - a) / (b - a)) if b > a else float(t >= a)


def lerp(a, b, u):
    return a + (b - a) * u


def smooth(u):
    u = clamp(u)
    return u * u * (3 - 2 * u)


def ease_out(u, p=3):
    u = clamp(u)
    return 1 - (1 - u) ** p


def ease_in(u, p=3):
    return clamp(u) ** p


def ease_in_out(u):
    u = clamp(u)
    return 4 * u ** 3 if u < .5 else 1 - (-2 * u + 2) ** 3 / 2


def back(u, s=1.6):
    u = clamp(u)
    return 1 + (s + 1) * (u - 1) ** 3 + s * (u - 1) ** 2


def pop(t, a, d=.35):
    """Scale-in curve that overshoots slightly; returns (alpha, scale)."""
    u = prog(t, a, a + d)
    return smooth(prog(t, a, a + d * .6)), lerp(.82, 1.0, back(u))


def window(t, a, b, fade_in=.25, fade_out=.25):
    return min(smooth(prog(t, a, a + fade_in)), 1 - smooth(prog(t, b - fade_out, b)))


# ------------------------------------------------------------------ fonts & text
@lru_cache(maxsize=None)
def F(size, weight='demi'):
    if weight == 'mono':
        return ImageFont.truetype(MONO, size)
    return ImageFont.truetype(AVENIR, size, index=WEIGHTS[weight])


def tw(s, size, weight='demi'):
    return F(size, weight).getlength(s)


@lru_cache(maxsize=6000)
def txt(s, size, weight='demi', color=INK, track=0):
    """Tight text sprite (straight alpha). Top of sprite = font ascender line."""
    f = F(size, weight)
    asc, desc = f.getmetrics()
    if track:
        width = sum(f.getlength(c) + track for c in s) - track
    else:
        width = f.getlength(s)
    w, h = max(1, int(math.ceil(width)) + 6), asc + desc + 4
    mask = Image.new('L', (w, h), 0)
    d = ImageDraw.Draw(mask)
    if track:
        x = 3
        for c in s:
            d.text((x, 2), c, font=f, fill=255)
            x += f.getlength(c) + track
    else:
        d.text((3, 2), s, font=f, fill=255)
    spr = Image.new('RGBA', (w, h), tuple(color[:3]) + (255,))
    spr.putalpha(mask)
    return spr


def wrap(s, size, weight, width):
    words, lines, cur = s.split(), [], ''
    for word in words:
        trial = (cur + ' ' + word).strip()
        if tw(trial, size, weight) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


# ------------------------------------------------------------------ compositing
_LUTS = {}


def fade(spr, a):
    k = int(clamp(a) * 255)
    if k >= 255:
        return spr
    lut = _LUTS.get(k)
    if lut is None:
        lut = _LUTS[k] = [v * k // 255 for v in range(256)]
    r, g, b, al = spr.split()
    return Image.merge('RGBA', (r, g, b, al.point(lut)))


def put(base, spr, x, y, alpha=1.0):
    if alpha <= .004 or spr is None:
        return
    x, y = int(round(x)), int(round(y))
    w, h = spr.size
    x0, y0, x1, y1 = max(0, x), max(0, y), min(base.width, x + w), min(base.height, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    if (x0, y0, x1, y1) != (x, y, x + w, y + h):
        spr = spr.crop((x0 - x, y0 - y, x1 - x, y1 - y))
    if alpha < .996:
        spr = fade(spr, alpha)
    base.alpha_composite(spr, (x0, y0))


def xform(spr, scale=1.0, rot=0.0, fast=True):
    if abs(scale - 1) > .002:
        w, h = spr.size
        spr = spr.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.BILINEAR if fast else Image.LANCZOS)
    if abs(rot) > .05:
        spr = spr.rotate(rot, Image.BILINEAR, expand=True)
    return spr


def putc(base, spr, cx, cy, alpha=1.0, scale=1.0, rot=0.0):
    spr = xform(spr, scale, rot)
    put(base, spr, cx - spr.width / 2, cy - spr.height / 2, alpha)


def text(base, s, x, y, size, weight='demi', color=INK, alpha=1.0, anchor='l', track=0):
    spr = txt(s, size, weight, tuple(color), track)
    if anchor == 'c':
        x -= spr.width / 2
    elif anchor == 'r':
        x -= spr.width
    put(base, spr, x - 3, y, alpha)
    return spr.width - 6


def typed(base, s, x, y, size, weight, color, frac, alpha=1.0, caret=None):
    """Draw first `frac` of s by cropping the full sprite at the prefix width."""
    n = int(round(len(s) * clamp(frac)))
    spr = txt(s, size, weight, tuple(color))
    cut = int(F(size, weight).getlength(s[:n])) + 3
    if n > 0:
        put(base, spr.crop((0, 0, min(spr.width, cut + 1), spr.height)), x - 3, y, alpha)
    if caret is not None and caret:
        asc, _ = F(size, weight).getmetrics()
        bar = solid(3, int(asc * .95), color)
        put(base, bar, x + cut - 1, y + 4, alpha)
    return cut


# ------------------------------------------------------------------ shapes
SS = 3


@lru_cache(maxsize=256)
def rr_mask(w, h, r):
    big = Image.new('L', (w * SS, h * SS), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), r * SS, fill=255)
    return big.resize((w, h), Image.LANCZOS)


@lru_cache(maxsize=128)
def ring_mask(w, h, r, width):
    big = Image.new('L', (w * SS, h * SS), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), r * SS, outline=255,
                                          width=width * SS)
    return big.resize((w, h), Image.LANCZOS)


@lru_cache(maxsize=512)
def solid(w, h, color, a=255):
    return Image.new('RGBA', (max(1, w), max(1, h)), tuple(color[:3]) + (a,))


def colorize(mask, color, a=255):
    spr = Image.new('RGBA', mask.size, tuple(color[:3]) + (255,))
    if a < 255:
        mask = mask.point(lambda v, a=a: v * a // 255)
    spr.putalpha(mask)
    return spr


@lru_cache(maxsize=256)
def rrect(w, h, r, color, a=255):
    return colorize(rr_mask(w, h, r), color, a)


@lru_cache(maxsize=128)
def ring(w, h, r, width, color, a=255):
    return colorize(ring_mask(w, h, r, width), color, a)


@lru_cache(maxsize=128)
def shadow(w, h, r, strength=46, blur=22, color=(18, 32, 64)):
    pad = blur * 2
    m = Image.new('L', (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad, pad + w, pad + h), r, fill=strength)
    m = m.filter(ImageFilter.GaussianBlur(blur))
    return colorize(m, color), pad


def card(base, x, y, w, h, r=26, color=WHITE, a=255, alpha=1.0, shadow_k=1.0, border=None,
         oy=12, blur=22):
    w, h = int(w), int(h)
    if shadow_k > 0:
        sh, pad = shadow(w, h, r, int(46 * shadow_k), blur)
        put(base, sh, x - pad, y - pad + oy, alpha)
    put(base, rrect(w, h, r, tuple(color), a), x, y, alpha)
    if border:
        put(base, ring(w, h, r, border[1], tuple(border[0]), border[2] if len(border) > 2 else 255),
            x, y, alpha)


@lru_cache(maxsize=128)
def card_sprite(w, h, r=26, color=WHITE, a=255, shadow_k=1.0, border=None):
    """Card rendered into its own sprite (with shadow margin). Returns (sprite, pad)."""
    sh, pad = shadow(w, h, r, int(46 * shadow_k), 22)
    spr = Image.new('RGBA', sh.size, (0, 0, 0, 0))
    spr.alpha_composite(sh, (0, 12))
    spr.alpha_composite(rrect(w, h, r, color, a), (pad, pad))
    if border:
        spr.alpha_composite(ring(w, h, r, border[1], border[0]), (pad, pad))
    return spr, pad


@lru_cache(maxsize=800)
def circle(d, color, a=255):
    return rrect(d, d, d // 2, color, a)


def pill(base, s, x, y, size=22, weight='demi', fg=WHITE, bg=TEAL, alpha=1.0, padx=16, h=None,
         outline=None, anchor='l', icon=None):
    width = int(tw(s, size, weight)) + 2 * padx + (size + 8 if icon else 0)
    h = h or int(size * 1.75)
    if anchor == 'c':
        x -= width / 2
    elif anchor == 'r':
        x -= width
    if bg is not None:
        put(base, rrect(width, h, h // 2, tuple(bg)), x, y, alpha)
    if outline:
        put(base, ring(width, h, h // 2, 2, tuple(outline)), x, y, alpha)
    tx = x + padx
    if icon:
        putc(base, icon, tx + size / 2, y + h / 2, alpha, scale=size / icon.height)
        tx += size + 8
    asc, desc = F(size, weight).getmetrics()
    text(base, s, tx, y + (h - asc - desc) / 2 - 2, size, weight, fg, alpha)
    return width


# ------------------------------------------------------------------ icons
@lru_cache(maxsize=64)
def icon(kind, size=64, color=TEAL):
    """Small vector icons drawn at 4x then downsampled."""
    S = size * 4
    m = Image.new('L', (S, S), 0)
    d = ImageDraw.Draw(m)
    lw = max(4, S // 14)
    if kind == 'check':
        d.ellipse((0, 0, S - 1, S - 1), fill=255)
        d.line([(S * .27, S * .52), (S * .44, S * .68), (S * .74, S * .34)], fill=0, width=lw + 4,
               joint='curve')
    elif kind == 'tick':
        d.line([(S * .14, S * .54), (S * .4, S * .78), (S * .88, S * .24)], fill=255, width=lw + 6,
               joint='curve')
    elif kind == 'doc':
        d.rounded_rectangle((S * .16, S * .04, S * .84, S * .96), S * .08, fill=255)
        d.polygon([(S * .6, S * .04), (S * .84, S * .04), (S * .84, S * .28)], fill=0)
        for k in range(3):
            d.rectangle((S * .28, S * (.46 + k * .14), S * .72, S * (.46 + k * .14) + lw * .7), fill=0)
    elif kind == 'sheet':
        d.rounded_rectangle((S * .08, S * .1, S * .92, S * .9), S * .08, fill=255)
        for k in range(1, 4):
            y = S * (.1 + k * .2)
            d.rectangle((S * .08, y, S * .92, y + lw * .6), fill=0)
        d.rectangle((S * .38, S * .1, S * .38 + lw * .6, S * .9), fill=0)
    elif kind == 'image':
        d.rounded_rectangle((S * .06, S * .14, S * .94, S * .86), S * .08, fill=255)
        d.polygon([(S * .14, S * .78), (S * .4, S * .44), (S * .58, S * .64), (S * .7, S * .52),
                   (S * .86, S * .78)], fill=0)
        d.ellipse((S * .62, S * .24, S * .76, S * .38), fill=0)
    elif kind == 'db':
        for k in range(3):
            y = S * (.12 + k * .26)
            d.ellipse((S * .14, y, S * .86, y + S * .26), fill=255)
            d.rectangle((S * .14, y + S * .13, S * .86, y + S * .3), fill=255)
            d.arc((S * .14, y + S * .04, S * .86, y + S * .3), 0, 180, fill=0, width=lw // 2)
    elif kind == 'mail':
        d.rounded_rectangle((S * .06, S * .2, S * .94, S * .8), S * .08, fill=255)
        d.line([(S * .1, S * .26), (S * .5, S * .56), (S * .9, S * .26)], fill=0, width=lw)
    elif kind == 'clock':
        d.ellipse((S * .06, S * .06, S * .94, S * .94), fill=255)
        d.line([(S * .5, S * .5), (S * .5, S * .22)], fill=0, width=lw)
        d.line([(S * .5, S * .5), (S * .7, S * .62)], fill=0, width=lw)
    elif kind == 'bell':
        d.pieslice((S * .2, S * .14, S * .8, S * .74), 180, 360, fill=255)
        d.rectangle((S * .2, S * .44, S * .8, S * .72), fill=255)
        d.rectangle((S * .12, S * .7, S * .88, S * .78), fill=255)
        d.ellipse((S * .42, S * .78, S * .58, S * .94), fill=255)
    elif kind == 'lock':
        d.rounded_rectangle((S * .18, S * .44, S * .82, S * .92), S * .08, fill=255)
        d.arc((S * .3, S * .1, S * .7, S * .66), 180, 360, fill=255, width=lw + 2)
        d.rectangle((S * .3, S * .36, S * .3 + lw + 2, S * .46), fill=255)
        d.rectangle((S * .7 - lw - 2, S * .36, S * .7, S * .46), fill=255)
    elif kind == 'calendar':
        d.rounded_rectangle((S * .08, S * .14, S * .92, S * .92), S * .08, fill=255)
        d.rectangle((S * .08, S * .3, S * .92, S * .34), fill=0)
        for k in range(3):
            for j in range(4):
                d.rectangle((S * (.18 + j * .18), S * (.44 + k * .15), S * (.28 + j * .18),
                             S * (.52 + k * .15)), fill=0)
    elif kind == 'person':
        d.ellipse((S * .3, S * .08, S * .7, S * .48), fill=255)
        d.pieslice((S * .1, S * .54, S * .9, S * 1.34), 180, 360, fill=255)
    elif kind == 'send':
        d.polygon([(S * .1, S * .14), (S * .92, S * .5), (S * .1, S * .86), (S * .24, S * .5)], fill=255)
    elif kind == 'spark':
        c = S / 2
        pts = []
        for k in range(8):
            r = S * (.48 if k % 2 == 0 else .14)
            a = k * math.pi / 4 - math.pi / 2
            pts.append((c + r * math.cos(a), c + r * math.sin(a)))
        d.polygon(pts, fill=255)
    elif kind == 'link':
        d.rounded_rectangle((S * .04, S * .32, S * .58, S * .68), S * .18, outline=255, width=lw)
        d.rounded_rectangle((S * .42, S * .32, S * .96, S * .68), S * .18, outline=255, width=lw)
    elif kind == 'arrow':
        d.line([(S * .1, S * .5), (S * .82, S * .5)], fill=255, width=lw)
        d.polygon([(S * .62, S * .26), (S * .94, S * .5), (S * .62, S * .74)], fill=255)
    m = m.resize((size, size), Image.LANCZOS)
    return colorize(m, color)


@lru_cache(maxsize=32)
def file_badge(kind, size=56):
    """Coloured file-type tile: PDF / XLSX / JPG / HTML."""
    colors = {'PDF': (214, 54, 48), 'XLSX': (33, 115, 70), 'JPG': (124, 82, 206),
              'HTML': (230, 120, 30), 'SCAN': (90, 104, 124)}
    spr = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    spr.alpha_composite(rrect(size, size, size // 5, colors[kind]))
    label = txt(kind, int(size * (.3 if len(kind) <= 3 else .24)), 'bold', WHITE)
    spr.alpha_composite(label, ((size - label.width) // 2, (size - label.height) // 2))
    return spr


@lru_cache(maxsize=4)
def cursor_sprite(scale=1.0):
    S = 4
    pts = [(0, 0), (0, 46), (11, 35), (19, 54), (27, 50), (19, 32), (34, 32)]
    big = Image.new('RGBA', (48 * S, 64 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.polygon([(x * S + 6, y * S + 6) for x, y in pts], fill=(20, 24, 32, 255))
    inner = [(x * S * .8 + 6 + 3.2 * S, y * S * .8 + 6 + 5 * S) for x, y in pts]
    d.polygon(inner, fill=(255, 255, 255, 255))
    spr = big.resize((int(48 * scale), int(64 * scale)), Image.LANCZOS)
    return spr


def cursor(base, x, y, alpha=1.0, click=None):
    """click: seconds since click (ripple) or None."""
    if click is not None and 0 <= click < .5:
        u = click / .5
        d = int(30 + 70 * ease_out(u))
        put(base, ring(d, d, d // 2, 4, TEAL), x - d / 2, y - d / 2, alpha * (1 - u))
    s = 1.0 - (.12 if click is not None and 0 <= click < .12 else 0)
    put(base, xform(cursor_sprite(), s), x - 2, y - 2, alpha)


# ------------------------------------------------------------------ images
@lru_cache(maxsize=512)
def load(rel, mode='RGBA'):
    path = ASSETS / rel
    return Image.open(path).convert(mode)


@lru_cache(maxsize=1024)
def fit(rel, w, h, mode='contain', bg=None):
    img = load(rel, 'RGBA')
    sw, sh = img.size
    if mode == 'cover':
        s = max(w / sw, h / sh)
        img = img.resize((max(1, int(sw * s)), max(1, int(sh * s))), Image.LANCZOS)
        x0, y0 = (img.width - w) // 2, (img.height - h) // 2
        return img.crop((x0, y0, x0 + w, y0 + h))
    s = min(w / sw, h / sh)
    img = img.resize((max(1, int(sw * s)), max(1, int(sh * s))), Image.LANCZOS)
    if bg is None:
        return img
    out = Image.new('RGBA', (w, h), tuple(bg) + (255,))
    out.alpha_composite(img, ((w - img.width) // 2, (h - img.height) // 2))
    return out


@lru_cache(maxsize=256)
def scaled(rel, width):
    img = load(rel)
    h = int(img.height * width / img.width)
    return img.resize((width, h), Image.LANCZOS)


@lru_cache(maxsize=256)
def rounded_img(rel, w, h, r=18, mode='cover', bg=WHITE):
    img = fit(rel, w, h, mode, bg if mode == 'contain' else None).copy()
    if img.size != (w, h):
        canvas = Image.new('RGBA', (w, h), tuple(bg) + (255,))
        canvas.alpha_composite(img, ((w - img.width) // 2, (h - img.height) // 2))
        img = canvas
    a = img.getchannel('A')
    from PIL import ImageChops
    img.putalpha(ImageChops.multiply(a, rr_mask(w, h, r)))
    return img


@lru_cache(maxsize=8)
def genie(kind='genie', height=200):
    img = load(f'brand/engenie-{kind}.png')
    bbox = img.getbbox()
    img = img.crop(bbox)
    w = int(img.width * height / img.height)
    return img.resize((w, height), Image.LANCZOS)


# ------------------------------------------------------------------ backgrounds
@lru_cache(maxsize=2)
def bg_light():
    y = np.linspace(0, 1, H)[:, None]
    x = np.linspace(0, 1, W)[None, :]
    top = np.array([234, 245, 246.])
    bot = np.array([248, 244, 233.])
    img = top * (1 - y[..., None]) + bot * y[..., None]
    img = np.broadcast_to(img, (H, W, 3)).copy()
    for cx, cy, rad, col, k in [(.12, .1, .55, (196, 232, 232), .55), (.92, .92, .5, (250, 230, 196), .5),
                                (.7, .2, .35, (226, 240, 250), .35)]:
        d = np.sqrt(((x - cx) * 1.7) ** 2 + (y - cy) ** 2) / rad
        wgt = (np.clip(1 - d, 0, 1) ** 2 * k)[..., None]
        img = img * (1 - wgt) + np.array(col, float) * wgt
    return Image.fromarray(img.clip(0, 255).astype(np.uint8)).convert('RGBA')


@lru_cache(maxsize=2)
def bg_dark():
    y = np.linspace(-1, 1, H)[:, None]
    x = np.linspace(-1.78, 1.78, W)[None, :]
    d = np.sqrt(x ** 2 + (y + .08) ** 2)
    inner = np.array([22, 46, 72.])
    outer = np.array([3, 8, 18.])
    k = np.clip(d / 1.9, 0, 1)[..., None] ** 1.2
    img = inner * (1 - k) + outer * k
    rng = np.random.default_rng(3)
    img += rng.normal(0, 1.4, img.shape)
    return Image.fromarray(img.clip(0, 255).astype(np.uint8)).convert('RGBA')


@lru_cache(maxsize=4)
def glow(d, color, strength=170):
    m = Image.new('L', (d, d), 0)
    ImageDraw.Draw(m).ellipse((d * .25, d * .25, d * .75, d * .75), fill=strength)
    m = m.filter(ImageFilter.GaussianBlur(d * .12))
    return colorize(m, color)


@lru_cache(maxsize=64)
def soft_rect_glow(w, h, r, color, blur=18, strength=200):
    pad = blur * 3
    m = Image.new('L', (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad, pad + w, pad + h), r, fill=strength)
    return colorize(m.filter(ImageFilter.GaussianBlur(blur)), color), pad


def line(base, pts, color, width=3, alpha=1.0, frac=1.0):
    """Polyline drawn on its own layer (antialiased via 2x supersample)."""
    if frac <= 0 or alpha <= 0:
        return
    pts = list(pts)
    if frac < 1:
        total = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        goal, acc, out = total * frac, 0.0, [pts[0]]
        for i in range(len(pts) - 1):
            seg = math.dist(pts[i], pts[i + 1])
            if acc + seg >= goal:
                u = (goal - acc) / seg if seg else 0
                out.append((lerp(pts[i][0], pts[i + 1][0], u), lerp(pts[i][1], pts[i + 1][1], u)))
                break
            out.append(pts[i + 1])
            acc += seg
        pts = out
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    pad = width * 2 + 4
    x0, y0 = int(min(xs)) - pad, int(min(ys)) - pad
    w, h = int(max(xs)) - x0 + pad, int(max(ys)) - y0 + pad
    m = Image.new('L', (w * 2, h * 2), 0)
    ImageDraw.Draw(m).line([((x - x0) * 2, (y - y0) * 2) for x, y in pts], fill=255,
                           width=width * 2, joint='curve')
    m = m.resize((w, h), Image.BILINEAR)
    put(base, colorize(m, color), x0, y0, alpha)


def bezier(p0, p1, p2, p3, n=40):
    out = []
    for i in range(n + 1):
        u = i / n
        a, b, c, d = (1 - u) ** 3, 3 * u * (1 - u) ** 2, 3 * u * u * (1 - u), u ** 3
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def overlay(base, color, a):
    if a <= .004:
        return
    base.alpha_composite(Image.new('RGBA', base.size, tuple(color[:3]) + (int(255 * clamp(a)),)))
