"""Brand introduction (old lockup + capability cards) and the 3D part view of the Mine Train C-frame."""
from __future__ import annotations

import json
import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageFilter

from common import *  # noqa: F401,F403
from scenes_a import header


@lru_cache(maxsize=8)
def old_frame(key, idx):
    n = MANIFEST['old_frames'][key]
    idx = max(0, min(n - 1, idx))
    return Image.open(ASSETS / f'old/{key}/{idx + 1:04d}.jpg').convert('RGBA')


# ================================================================== brand
BRAND_T0, BRAND_CLIP = 12.85, 2.0
LOCKUP_BOX = (228, 256, 1808, 764)
CAPS = [('Find', 'Any file, any format', 'doc', TEAL, 16.45),
        ('Verify', 'Cross-checked, with sources', 'check', GREEN, 17.14),
        ('Deliver', 'Reports and alerts, on time', 'send', PURPLE, 17.95)]


@lru_cache(maxsize=1)
def brand_layers():
    """Split the last lockup frame into a clean background plate and a lockup sprite."""
    last = old_frame('brand', 10 ** 6).convert('RGB')
    a = np.asarray(last).astype(np.float32)
    x0, y0, x1, y1 = LOCKUP_BOX
    masked = a.copy()
    masked[y0:y1, x0:x1] = np.median(a.reshape(-1, 3), 0)
    small = Image.fromarray(masked.astype(np.uint8)).resize((24, 14), Image.BILINEAR)
    plate = small.filter(ImageFilter.GaussianBlur(1.2)).resize((W, H), Image.BICUBIC)
    p = np.asarray(plate).astype(np.float32)
    diff = np.abs(a - p).sum(2)[y0:y1, x0:x1]
    alpha = np.clip((diff - 8) / 36, 0, 1)
    alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(.6)))
    lock = Image.fromarray(a[y0:y1, x0:x1].astype(np.uint8)).convert('RGBA')
    lock.putalpha(Image.fromarray(alpha))
    return plate.convert('RGBA'), lock


@lru_cache(maxsize=4)
def cap_card(i):
    title, sub, ic, col, _ = CAPS[i]
    w, h = 440, 168
    spr, pad = card_sprite(w, h, 28)
    spr = spr.copy()
    put(spr, circle(68, col), pad + 30, pad + 28)
    putc(spr, icon(ic, 38, WHITE), pad + 64, pad + 62)
    text(spr, title, pad + 120, pad + 34, 44, 'heavy', INK)
    text(spr, sub, pad + 32, pad + 116, 23, 'medium', INK2)
    return spr


def brand(t):
    k = int(round((t - BRAND_T0) * FPS))
    if k < MANIFEST['old_frames']['brand']:
        return old_frame('brand', k).copy()
    plate, lock = brand_layers()
    base = plate.copy()
    u = ease_in_out(prog(t, 15.25, 16.15))
    push = 1 + .025 * smooth(prog(t, BRAND_T0 + BRAND_CLIP, 19.2))
    x0, y0, x1, y1 = LOCKUP_BOX
    cx0, cy0 = (x0 + x1) / 2, (y0 + y1) / 2
    putc(base, lock, lerp(cx0, 960, u), lerp(cy0, 330, u), 1.0, lerp(1.0, .74, u) * push)
    for i, (*_, t0) in enumerate(CAPS):
        a, sc = pop(t, t0 - .12, .4)
        if a > 0:
            putc(base, cap_card(i), 270 + 220 + i * 470, 680, a, sc)
    return base


# ================================================================== 3D part view
P3D_T0 = 36.85
P3D_DIR = ASSETS / 'model3d'
VIEW = (134, 200, 1150, 694)
PANEL_X = 1318
PATH = [('Seven Dwarfs Mine Train', .25), ('Coach 1', 1.25), ('Left-front wheel carrier', 1.95),
        ('C-Frame', 2.55)]
DETAILS = [('doc', 'Drawing SDLDWARMV0217'), ('check', 'Crack test every 240,000 laps'),
           ('doc', '3 MRB records · all closed')]


@lru_cache(maxsize=1)
def p3d_meta():
    return json.loads((P3D_DIR / 'meta.json').read_text())['frames']


@lru_cache(maxsize=6)
def p3d_frame(i):
    i = max(0, min(len(p3d_meta()) - 1, i))
    _, _, w, h = VIEW
    img = Image.open(P3D_DIR / f'{i:04d}.png').convert('RGB').resize((w, h), Image.LANCZOS).convert('RGBA')
    img.putalpha(rr_mask(w, h, 18))
    return img


def part3d(t):
    base = bg_light().copy()
    T0 = P3D_T0
    header(base, t, T0, '01', 'VERSATILITY', 'Right down to the part, in 3D')
    x, y, w, h = 110, 120, 1700, 790
    card(base, x, y, w, h, 30)
    put(base, circle(46, TEAL_L), x + 26, y + 18)
    putc(base, icon('spark', 26, TEAL), x + 49, y + 41)
    text(base, 'Equipment models', x + 88, y + 20, 26, 'demi', INK)
    text(base, 'Seven Dwarfs Mine Train · vehicle', x + 330, y + 26, 20, 'medium', MUTED)
    pill(base, '3D view', x + w - 30, y + 20, 18, 'demi', TEAL_D, TEAL_L, anchor='r')
    vx, vy, vw, vh = VIEW
    f = int((t - T0) * FPS)
    put(base, p3d_frame(f), vx, vy)
    put(base, ring(vw, vh, 18, 2, LINE), vx, vy)
    # callout on the highlighted part
    meta = p3d_meta()[max(0, min(len(p3d_meta()) - 1, f))]
    ca = smooth(prog(t, T0 + 2.6, T0 + 2.9))
    if ca > 0:
        px, py = vx + meta['x'] * vw, vy + meta['y'] * vh
        r = 26 + 6 * math.sin((t - T0) * 5)
        put(base, ring(int(2 * r), int(2 * r), int(r), 3, TEAL), px - r, py - r, ca)
        lx, ly = min(px + 210, vx + vw - 430), py - 210
        line(base, [(px + 18, py - 18), (lx - 10, ly + 26)], TEAL, 3, ca, ease_out(prog(t, T0 + 2.6, T0 + 2.95)))
        la, ls = pop(t, T0 + 2.85, .35)
        spr = Image.new('RGBA', (420, 60), (0, 0, 0, 0))
        put(spr, rrect(420, 60, 30, TEAL), 0, 0)
        text(spr, 'C-Frame · SDLDWARMV0217', 26, 13, 25, 'demi', WHITE)
        putc(base, spr, lx + 200, ly + 26, la, ls)
    # part locator panel
    X = PANEL_X
    text(base, 'PART LOCATOR', X, vy + 6, 18, 'demi', TEAL, smooth(prog(t, T0, T0 + .3)), track=3)
    for k, (label, dt) in enumerate(PATH):
        a = smooth(prog(t, T0 + dt, T0 + dt + .25))
        if a <= 0:
            continue
        ry = vy + 52 + k * 58
        last = k == len(PATH) - 1
        if k:
            put(base, solid(3, 30, LINE), X + 11, ry - 32, a)
        put(base, circle(24, TEAL if last else (196, 206, 214)), X, ry + 4, a)
        text(base, label, X + 40, ry, 24 if last else 22, 'demi' if last else 'medium',
             TEAL_D if last else INK2, a)
    da = smooth(prog(t, T0 + 2.8, T0 + 3.1))
    if da > 0:
        dy = vy + 300 + 24 * (1 - ease_out(prog(t, T0 + 2.8, T0 + 3.2)))
        card(base, X - 10, dy, 470, 300, 22, color=(247, 250, 251), alpha=da, shadow_k=.4, border=(LINE, 2))
        text(base, 'C-FRAME · COACH 1', X + 18, dy + 22, 17, 'demi', MUTED, da, track=2)
        text(base, 'SDLDWARMV0217', X + 18, dy + 50, 38, 'heavy', INK, da)
        for k, (ic, label) in enumerate(DETAILS):
            ra = smooth(prog(t, T0 + 3.0 + k * .12, T0 + 3.25 + k * .12))
            put(base, icon(ic, 24, TEAL), X + 20, dy + 128 + k * 52, da * ra)
            text(base, label, X + 56, dy + 124 + k * 52, 21, 'medium', INK2, da * ra)
    return base
