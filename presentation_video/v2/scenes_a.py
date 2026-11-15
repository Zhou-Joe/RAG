"""Scenes 1-6: wow opening, any file, cross-check, tracker, picture search."""
from __future__ import annotations

import math
import random
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from common import *  # noqa: F401,F403
from common import MANIFEST


# ================================================================== shared UI
def header(base, t, t0, num, kicker, title):
    a = smooth(prog(t, t0, t0 + .4))
    if a <= 0:
        return
    put(base, circle(52, TEAL), 104, 46, a)
    text(base, num, 130, 58, 22, 'demi', WHITE, a, anchor='c')
    text(base, kicker, 174, 44, 18, 'demi', TEAL, a, track=3)
    text(base, title, 174, 66, 30, 'demi', INK, a)


def genie_card(base, t, t0, x=110, y=250, w=230, h=330, bounce=None):
    a = smooth(prog(t, t0, t0 + .35))
    card(base, x, y, w, h, 30, alpha=a)
    g = genie('genie', 210)
    s = 1.0
    if bounce is not None and 0 <= t - bounce < .45:
        u = (t - bounce) / .45
        s = 1 + .08 * math.sin(u * math.pi) * (1 - u)
    putc(base, g, x + w / 2, y + 130, a, scale=s)
    text(base, 'En-Genie', x + w / 2, y + 262, 26, 'demi', INK, a, anchor='c')
    text(base, 'AI agent', x + w / 2, y + 296, 18, 'medium', MUTED, a, anchor='c')


def chat_shell(base, t, t0, title='Ask En-Genie', tag=None, tag_colors=(GOLD_L, (150, 96, 10)),
               x=380, y=110, w=1430, h=790):
    a = smooth(prog(t, t0, t0 + .35))
    dy = 18 * (1 - ease_out(prog(t, t0, t0 + .45)))
    card(base, x, y + dy, w, h, 32, alpha=a)
    put(base, genie('front', 40), x + 36, y + 22 + dy, a)
    text(base, title, x + 88, y + 26 + dy, 28, 'demi', INK, a)
    if tag:
        pill(base, tag, x + w - 40, y + 26 + dy, 20, 'demi', tag_colors[1], tag_colors[0], a, anchor='r')
    put(base, solid(w - 72, 2, LINE), x + 36, y + 84 + dy, a)
    return a


def user_bubble(base, t, lines, x_right, y, t_type0, t_type1, size=28, alpha=1.0):
    widths = [tw(l, size, 'medium') for l in lines]
    w = int(max(widths)) + 48
    lh = int(size * 1.45)
    h = lh * len(lines) + 30
    a = alpha * smooth(prog(t, t_type0 - .25, t_type0))
    put(base, rrect(w, h, 24, TEAL_L), x_right - w, y, a)
    total = sum(len(l) for l in lines)
    done = total * prog(t, t_type0, t_type1)
    caret_on = t < t_type1 + .3 and (t < t_type1 or int(t * 4) % 2 == 0)
    for i, l in enumerate(lines):
        frac = clamp(done / max(1, len(l)))
        done -= len(l)
        show_caret = caret_on and (0 < frac < 1 or (frac >= 1 and i == len(lines) - 1))
        if frac > 0 or i == 0:
            typed(base, l, x_right - w + 24, y + 14 + i * lh, size, 'medium', INK, frac, a,
                  caret=show_caret)
    return w, h


@lru_cache(maxsize=64)
def chip_sprite(kind, title, sub, w=470, h=84, accent=TEAL):
    spr, pad = card_sprite(w, h, 18, WHITE, 255, .7, (LINE, 2))
    spr = spr.copy()
    put(spr, rrect(6, h - 28, 3, accent), pad + 10, pad + 14)
    put(spr, file_badge(kind, 50), pad + 26, pad + (h - 50) // 2)
    text(spr, title, pad + 92, pad + 12, 24, 'demi', INK)
    text(spr, sub, pad + 92, pad + 46, 19, 'medium', MUTED)
    return spr, pad


def chip_at(base, kind, title, sub, x, y, alpha=1.0, scale=1.0, w=470, h=84):
    spr, pad = chip_sprite(kind, title, sub, w, h)
    if abs(scale - 1) < .003:
        put(base, spr, x - pad, y - pad, alpha)
    else:
        putc(base, spr, x + w / 2, y + h / 2, alpha, scale)


def source_pill(base, kind, label, x, y, alpha=1.0, glow_k=0.0, size=21):
    width = int(tw(label, size, 'demi')) + 84
    h = 52
    if glow_k > 0:
        g, pad = soft_rect_glow(width, h, 26, TEAL, 12, 220)
        put(base, g, x - pad, y - pad, alpha * glow_k)
    put(base, rrect(width, h, 26, WHITE), x, y, alpha)
    put(base, ring(width, h, 26, 2, LINE if glow_k < .5 else TEAL), x, y, alpha)
    put(base, file_badge(kind, 34), x + 10, y + 9, alpha)
    text(base, label, x + 56, y + 11, size, 'demi', INK, alpha)
    return width


def caption_safe():
    return 905


# ================================================================== S1 · wow opening
LABELS = [(0.32, 'PDF', 'Manuals', 'Ride maintenance'), (0.92, 'PDF', 'Standards', 'Disney · Vekoma'),
          (1.72, 'XLSX', 'Drawings', 'Component lists'), (2.42, 'SCAN', 'Lab reports', 'Scanned oil tests')]
SEARCH_Q = 'Which attraction needs immediate attention? Show me the proof.'
STORM_C = (960, 520)
RX, RY = 1010, 560


@lru_cache(maxsize=8)
def label_sprite(kind, word, sub):
    w, h = 372, 132
    spr, pad = card_sprite(w, h, 28, (10, 22, 42), 222, 1.4, ((90, 190, 196), 2))
    spr = spr.copy()
    put(spr, file_badge(kind, 58), pad + 26, pad + (h - 58) // 2)
    text(spr, word, pad + 104, pad + 22, 44, 'heavy', WHITE)
    text(spr, sub, pad + 106, pad + 80, 22, 'demi', (150, 212, 220))
    return spr


def paper(img, width, border=(206, 212, 220)):
    img = img.convert('RGB')
    h = int(img.height * width / img.width)
    img = img.resize((width, h), Image.LANCZOS)
    pad = 16
    spr = Image.new('RGBA', (width + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    m = Image.new('L', spr.size, 0)
    ImageDraw.Draw(m).rectangle((pad, pad + 5, pad + width, pad + h + 5), fill=150)
    spr.alpha_composite(colorize(m.filter(ImageFilter.GaussianBlur(7)), (0, 0, 0)))
    spr.paste(img, (pad, pad))
    ImageDraw.Draw(spr).rectangle((pad, pad, pad + width - 1, pad + h - 1), outline=border + (255,))
    return spr


@lru_cache(maxsize=1)
def storm():
    rng = random.Random(11)
    files = [('storm/' + s['file'], 200) for s in MANIFEST['storm_pdfs']]
    for n in MANIFEST['storm_sheets']:
        if load('storm/' + n).width > 500:
            files.append(('storm/' + n, 320))
    files += [('storm/photo_tron.jpg', 300), ('storm/photo_minetrain.jpg', 300)]
    files += [('figs/' + n, 210) for n in MANIFEST['figs'][:12]]
    rng.shuffle(files)
    items = []
    n = len(files)
    us = sorted(math.sqrt(rng.uniform(.03, 1.0)) * .95 + .12 for _ in range(n))
    for i, (rel, width) in enumerate(files):
        u = us[i]
        z = rng.uniform(.6, 1.25)
        rot = rng.uniform(-16, 16)
        spr = paper(load(rel, 'RGB'), width)
        spr = spr.rotate(rot, Image.BICUBIC, expand=True)
        items.append(dict(spr=spr, theta=rng.uniform(0, math.tau), u=u, z=z, rot=rot,
                          omega=.05 + .09 * z, appear=1.05 + 1.5 * (i / n) + rng.uniform(0, .25),
                          delay=rng.uniform(0, .18)))
    items.sort(key=lambda it: it['z'])
    return items


@lru_cache(maxsize=1)
def cover_big():
    return paper(load('pdf/mt_cover.jpg', 'RGB'), 540)


COVER_SLOT = dict(theta=-2.25, u=.36, z=1.18, omega=.12)


def storm_pos(it, t):
    tau = max(0.0, min(t, 5.7) - 1.0)
    grow = ease_out(prog(t, it['appear'], it['appear'] + 1.1), 3)
    theta = it['theta'] + it['omega'] * tau
    g = lerp(1.16, .98, ease_out(prog(t, 1.0, 5.7), 2))
    r = it['u'] * grow
    x = STORM_C[0] + math.cos(theta) * r * RX * g
    y = STORM_C[1] + math.sin(theta) * r * RY * g
    return x, y, it['z'] * g * .95, theta, r


def vortex(x, y, scale, t, delay):
    v = ease_in(prog(t, 7.28 + delay, 7.95), 2.4)
    if v <= 0:
        return x, y, scale, 0.0, 1.0
    cx, cy = 960, 540
    dx, dy = x - cx, y - cy
    r = math.hypot(dx, dy) * (1 - v)
    a = math.atan2(dy, dx) + v * 2.6
    return cx + r * math.cos(a), cy + r * math.sin(a), scale * (1 - .88 * v), v * 160, 1 - v ** 3


def s1(t):
    base = bg_dark().copy()
    # spotlight on the first manual
    spot = 1 - smooth(prog(t, .9, 1.8))
    if spot > 0:
        putc(base, glow(1300, (70, 140, 170), 120), 960, 520, spot * smooth(prog(t, 0, .5)))
    if t < 8.0:
        items = storm()
        dim_storm = smooth(prog(t, 5.7, 5.95)) * .66
        for it in items:
            if t < it['appear']:
                continue
            x, y, s, _, r = storm_pos(it, t)
            x, y, s, extra_rot, va = vortex(x, y, s, t, it['delay'])
            a = smooth(prog(t, it['appear'], it['appear'] + .35)) * va
            a *= .62 + .38 * clamp((it['z'] - .6) / .65)
            if a <= .01 or s < .03:
                continue
            spr = xform(it['spr'], s, extra_rot)
            put(base, spr, x - spr.width / 2, y - spr.height / 2, a)
        # the hero manual cover: big -> joins the storm
        slot = dict(COVER_SLOT, appear=1.0)
        if t < 1.95:
            cb = cover_big()
            if t < 1.0:
                s = lerp(.96, 1.0, ease_out(t / 1.0))
                putc(base, cb, 960, 515, smooth(prog(t, 0, .35)), s)
            else:
                u = ease_in_out(prog(t, 1.0, 1.95))
                x1, y1, s1_, _, _ = storm_pos(dict(slot, appear=.0), t)
                target = s1_ * 200 / 540
                putc(base, cb, lerp(960, x1, u), lerp(515, y1, u), 1.0, lerp(1.0, target, u))
        else:
            x, y, s, _, _ = storm_pos(dict(slot, appear=.0), t)
            x, y, s, extra, va = vortex(x, y, s * 200 / 540, t, .05)
            if va > .01:
                putc(base, cover_big(), x, y, va, s, extra)
        if dim_storm > 0:
            overlay(base, (4, 10, 20), dim_storm)
        # document types, timed to the narration
        out = 1 - smooth(prog(t, 5.45, 5.75))
        for i, (t0, kind, word, sub) in enumerate(LABELS):
            if t < t0 or out <= 0:
                continue
            a, sc = pop(t, t0, .4)
            cx = 960 + (i - 1.5) * 410
            putc(base, label_sprite(kind, word, sub), cx, 560, a * out, sc)
        # search bar
        if t >= 5.72:
            a = smooth(prog(t, 5.72, 6.0)) * (1 - smooth(prog(t, 7.95, 8.1)))
            sc = lerp(.95, 1.0, ease_out(prog(t, 5.72, 6.05))) + .03 * smooth(prog(t, 7.4, 7.9))
            bw, bh = 1280, 104
            bar = Image.new('RGBA', (bw + 160, bh + 160), (0, 0, 0, 0))
            k = smooth(prog(t, 7.12, 7.3)) * (1 - .4 * smooth(prog(t, 7.5, 7.9)))
            if k > 0:
                g, pad = soft_rect_glow(bw, bh, 52, (40, 200, 210), 22, 255)
                put(bar, g, 80 - pad, 80 - pad, k)
            put(bar, rrect(bw, bh, 52, WHITE), 80, 80)
            put(bar, genie('front', 62), 110, 80 + 21)
            frac = prog(t, 5.95, 7.05)
            caret = t < 7.25 and (frac < 1 or int(t * 4) % 2 == 0)
            if frac <= 0:
                text(bar, 'Ask En-Genie anything…', 194, 80 + 30, 33, 'medium', MUTED)
            typed(bar, SEARCH_Q, 194, 80 + 30, 33, 'medium', INK, frac, 1.0, caret=caret)
            put(bar, circle(64, TEAL), 80 + bw - 84, 80 + 20)
            putc(bar, icon('send', 30, WHITE), 80 + bw - 52, 80 + 52)
            putc(base, bar, 960, 540, a, sc)
            text(base, 'Ask En-Genie', 960, 430, 24, 'demi', (170, 214, 222), a * .9, anchor='c', track=2)
    else:
        s1_answer(base, t)
    # flash
    fl = smooth(prog(t, 7.78, 7.95)) * (1 - smooth(prog(t, 7.95, 8.4)))
    overlay(base, WHITE, .92 * fl)
    return base


@lru_cache(maxsize=1)
def oil_page():
    s = 860 / MANIFEST['oil_size'][1]
    img = load('pdf/oil_p1.png', 'RGB')
    img = img.resize((int(img.width * s), 860), Image.LANCZOS).convert('RGBA')
    return img, s


def s1_answer(base, t):
    putc(base, glow(1400, (30, 110, 130), 90), 560, 500, smooth(prog(t, 8.0, 8.6)))
    page, s = oil_page()
    px, py = 1150, 92
    a = smooth(prog(t, 8.05, 8.45))
    dy = 34 * (1 - ease_out(prog(t, 8.05, 8.6)))
    sh, pad = shadow(page.width, page.height, 6, 120, 24, (0, 0, 0))
    put(base, sh, px - pad, py + dy - pad + 14, a)
    put(base, page, px, py + dy, a)
    # highlight the diagnosis box and the water trend
    d = [v * s for v in MANIFEST['oil_diag']]
    k = smooth(prog(t, 8.55, 8.85))
    if k > 0:
        pulse = .6 + .4 * math.sin((t - 8.55) * 7)
        w_, h_ = int(d[2] - d[0]) + 12, int(d[3] - d[1]) + 12
        g, gp = soft_rect_glow(w_, h_, 8, RED, 14, 255)
        put(base, g, px + d[0] - 6 - gp, py + dy + d[1] - 6 - gp, k * pulse * .8)
        put(base, ring(w_, h_, 8, 5, RED), px + d[0] - 6, py + dy + d[1] - 6, k)
    gr = [v * s for v in MANIFEST['oil_graph']]
    k2 = smooth(prog(t, 9.05, 9.3))
    if k2 > 0:
        w_, h_ = int(gr[2] - gr[0]) + 10, int(gr[3] - gr[1]) + 10
        put(base, ring(w_, h_, 8, 4, GOLD), px + gr[0] - 5, py + dy + gr[1] - 5, k2)
    # popped-out water trend graph
    k3 = prog(t, 9.15, 9.75)
    if k3 > 0:
        gimg = scaled('img/water_graph.jpg', 540)
        gx, gy = 1010, 655
        card(base, gx - 14, gy - 14, gimg.width + 28, gimg.height + 28, 18,
             alpha=smooth(prog(t, 9.15, 9.35)), border=(GOLD, 3))
        cut = int(gimg.width * ease_out(k3, 2))
        if cut > 2:
            put(base, gimg.crop((0, 0, cut, gimg.height)), gx, gy)
    # answer card
    x, y, w, h = 120, 214, 860, 560
    a = smooth(prog(t, 8.1, 8.4))
    dx = -30 * (1 - ease_out(prog(t, 8.1, 8.6)))
    card(base, x + dx, y, w, h, 32, alpha=a, shadow_k=2.0)
    X = x + dx + 44
    put(base, genie('front', 50), X, y + 30, a)
    text(base, 'En-Genie', X + 64, y + 38, 26, 'demi', INK, a)
    pill(base, 'Answer · with proof', x + dx + w - 40, y + 34, 20, 'demi', TEAL_D, TEAL_L, a, anchor='r')
    put(base, solid(w - 88, 2, LINE), X, y + 100, a)
    a1 = smooth(prog(t, 8.25, 8.5))
    text(base, 'Slinky Dog Spin', X, y + 122, 50, 'demi', INK, a1)
    text(base, 'Kicker Wheel #1 · gearbox oil', X, y + 190, 30, 'medium', INK2, a1)
    a2, s2 = pop(t, 8.45, .35)
    if a2 > 0:
        pw = int(tw('RED · needs immediate attention', 26, 'demi')) + 40
        spr = Image.new('RGBA', (pw, 50), (0, 0, 0, 0))
        put(spr, rrect(pw, 50, 25, RED), 0, 0)
        text(spr, 'RED · needs immediate attention', 20, 9, 26, 'demi', WHITE)
        putc(base, spr, X + pw / 2, y + 268, a2, s2)
    a3 = smooth(prog(t, 8.6, 8.8))
    v = int(4215 * ease_out(prog(t, 8.6, 9.2), 3))
    nw = text(base, f'{v:,}', X, y + 314, 96, 'heavy', RED, a3)
    text(base, 'ppm water in oil', X + max(nw, 250) + 18, y + 372, 30, 'demi', INK, a3)
    a4 = smooth(prog(t, 8.95, 9.2))
    text(base, 'Control limit 500 ppm', X, y + 438, 26, 'medium', INK2, a4)
    pill(base, '8× over', X + 300, y + 434, 22, 'demi', (130, 80, 0), GOLD_L, a4)
    a5 = smooth(prog(t, 9.2, 9.45))
    sw = source_pill(base, 'PDF', 'Source · SGS lab report 32868274 · page 1', X, y + 488, a5,
                     glow_k=smooth(prog(t, 9.4, 9.8)))
    # connector from source to the scan
    k4 = prog(t, 9.4, 9.85)
    if k4 > 0:
        p0 = (X + sw + 4, y + 514)
        p3 = (px + d[0] - 8, py + dy + (d[1] + d[3]) / 2)
        pts = bezier(p0, (p0[0] + 140, p0[1]), (p3[0] - 160, p3[1]), p3)
        line(base, pts, TEAL, 4, a5, ease_out(k4, 2))
        if k4 >= 1:
            putc(base, circle(14, TEAL), p3[0], p3[1])


# ================================================================== S3 · any file
FILE_CARDS = [
    ('pdf/mt_cover.jpg', 'Manuals', 'Ride manuals · PDF'),
    ('STANDARD', 'Standards', 'Disney & Vekoma specs'),
    ('storm/sheet_0.png', 'Spreadsheets', 'Drawing lists · Excel'),
    ('pdf/oil_p1.png', 'Scanned reports', 'SGS lab scans'),
    ('img/photo_2.jpg', 'Photos & figures', 'JPG · PNG'),
]
PAGE_X, PAGE_Y, PAGE_W, PAGE_H = 380, 104, 594, 840
INFO_X = 1330


def _std_rel():
    for s in MANIFEST['storm_pdfs']:
        if s['tag'] == 'standard':
            return 'storm/' + s['file']
    return 'storm/' + MANIFEST['storm_pdfs'][0]['file']


def file_card_rect(i):
    cx = 960 + (i - 2) * 292
    return cx - 125, 300, 250, 330


def s3(t):
    base = bg_light().copy()
    header(base, t, 13.0, '01', 'VERSATILITY', 'Any file becomes knowledge')
    # ---- phase A: file types drop into one knowledge base
    if t < 15.0:
        out = smooth(prog(t, 14.45, 14.85))
        for i, (rel, label, sub) in enumerate(FILE_CARDS):
            rel = _std_rel() if rel == 'STANDARD' else rel
            x, y, w, h = file_card_rect(i)
            a, sc = pop(t, 13.05 + i * .09, .45)
            if i == 0 and t >= 14.45:
                continue
            fade_k = 1 - out
            dy = 40 * out
            spr, pad = card_sprite(w, h, 24)
            spr = spr.copy()
            put(spr, fit(rel, 226, 226, 'contain', (244, 246, 249)), pad + 12, pad + 12)
            text(spr, label, pad + w / 2, pad + 250, 26, 'demi', INK, anchor='c')
            text(spr, sub, pad + w / 2, pad + 288, 19, 'medium', MUTED, anchor='c')
            putc(base, spr, x + w / 2, y + h / 2 + dy, a * fade_k, sc)
        # knowledge base bar
        ka = smooth(prog(t, 13.6, 13.9)) * (1 - out)
        if ka > 0:
            bx, by, bw, bh = 300, 712, 1320, 76
            fill = smooth(prog(t, 13.85, 14.35))
            card(base, bx, by, bw, bh, 38, alpha=ka, border=(TEAL, 2, 160))
            if fill > 0:
                put(base, rrect(max(76, int(bw * fill)), bh, 38, TEAL_L), bx, by, ka)
            put(base, genie('front', 46), bx + 24, by + 15, ka)
            text(base, 'One searchable knowledge base', bx + bw / 2, by + 20, 28, 'demi', TEAL_D, ka,
                 anchor='c')
            for i in range(5):
                x, y, w, h = file_card_rect(i)
                u = prog(t, 13.75 + i * .07, 14.15 + i * .07)
                if 0 < u < 1:
                    yy = lerp(y + h, by + 10, ease_in(u, 2))
                    putc(base, circle(16, TEAL), x + w / 2, yy, ka * (1 - u * .3))
        # hero manual card morphs into the page view
        if t >= 14.45:
            u = ease_in_out(prog(t, 14.45, 14.95))
            x, y, w, h = file_card_rect(0)
            img = fit('pdf/mt_cover.jpg', 226, 226, 'contain')
            ix, iy = x + 12 + (226 - img.width) / 2, y + 12
            tw_ = lerp(img.width, PAGE_W, u)
            th_ = lerp(img.height, PAGE_H, u)
            big = load('pdf/mt_cover.jpg').resize((int(tw_), int(th_)), Image.BILINEAR)
            sh, pad = shadow(int(tw_), int(th_), 6, 70, 20)
            put(base, sh, lerp(ix, PAGE_X, u) - pad, lerp(iy, PAGE_Y, u) - pad + 12)
            put(base, big, lerp(ix, PAGE_X, u), lerp(iy, PAGE_Y, u))
    # ---- phase B: flip to page 390
    if 14.95 <= t < 17.75:
        a_out = 1 - smooth(prog(t, 17.3, 17.65))
        dx = -260 * ease_in(prog(t, 17.3, 17.7), 2)
        flips = MANIFEST['flip_pages']
        n = len(flips)
        u = ease_in_out(prog(t, 14.95, 16.05)) * (n - 1)
        i = min(int(u), n - 2)
        frac = u - i if u < n - 1 else 1.0
        sh, pad = shadow(PAGE_W, PAGE_H, 6, 70, 20)
        put(base, sh, PAGE_X + dx - pad, PAGE_Y - pad + 12, a_out)
        for k in range(4, 0, -1):
            put(base, solid(PAGE_W, PAGE_H, (226 - k * 6, 230 - k * 6, 236 - k * 6)), PAGE_X + dx + k * 3,
                PAGE_Y + k * 3, a_out)
        if t < 16.05:
            nxt = scaled(f'flip/{i + 1:02d}.jpg', PAGE_W)
            cur = scaled(f'flip/{i:02d}.jpg', PAGE_W)
            put(base, nxt, PAGE_X + dx, PAGE_Y, a_out)
            cw = int(PAGE_W * (1 - frac))
            if cw > 2:
                sq = cur.resize((cw, cur.height), Image.BILINEAR)
                put(base, sq, PAGE_X + dx, PAGE_Y, a_out)
                put(base, solid(cw, PAGE_H, (0, 0, 0), int(70 * frac)), PAGE_X + dx, PAGE_Y, a_out)
            page_no = int(lerp(flips[i], flips[i + 1], frac)) + 1
        else:
            p390 = scaled('pdf/mt_p390.png', PAGE_W)
            put(base, p390, PAGE_X + dx, PAGE_Y, a_out)
            page_no = 390
            sc = PAGE_W / MANIFEST['p390_size'][0]
            r = [v * sc for v in MANIFEST['p390_row']]
            k = ease_out(prog(t, 16.1, 16.45))
            if k > 0:
                rw, rh = int((r[2] - r[0]) * k) + 10, int(r[3] - r[1]) + 8
                put(base, rrect(rw, rh, 6, GOLD, 70), PAGE_X + dx + r[0] - 5, PAGE_Y + r[1] - 4, a_out)
                put(base, ring(rw, rh, 6, 3, GOLD), PAGE_X + dx + r[0] - 5, PAGE_Y + r[1] - 4, a_out)
            # magnified row so the rule is readable
            la = smooth(prog(t, 16.2, 16.45)) * (1 - smooth(prog(t, 16.55, 16.75)))
            if la > 0:
                lens = row_lens()
                lx = PAGE_X + dx + (r[0] + r[2]) / 2
                ly = PAGE_Y + r[3] + 30 + lens.height / 2
                putc(base, lens, lx, ly, la * a_out, lerp(.85, 1.0, ease_out(prog(t, 16.2, 16.5))))
        # info column
        ia = smooth(prog(t, 14.75, 15.1)) * a_out
        text(base, 'MAINTENANCE MANUAL', INFO_X, 160, 19, 'demi', TEAL, ia, track=3)
        text(base, 'Seven Dwarfs Mine Train', INFO_X, 190, 36, 'demi', INK, ia)
        text(base, 'Ride maintenance manual · PDF', INFO_X, 244, 24, 'medium', INK2, ia)
        text(base, f'p. {page_no}', INFO_X, 290, 88, 'heavy', TEAL, ia)
    # ---- phase C: spreadsheet scroll to row 2,620
    if t >= 17.3:
        s3_sheet(base, t)
    # ---- evidence tray
    ea = smooth(prog(t, 16.5, 16.8))
    if ea > 0:
        text(base, 'EVIDENCE FOUND', INFO_X, 560, 19, 'demi', MUTED, ea, track=3)
    cx_, cy_ = p390_row_center()
    chip_fly(base, t, 16.55, 17.15, 'PDF', 'Inspection rule', 'Manual · page 390', (cx_, cy_ + 110),
             (INFO_X, 600))
    chip_fly(base, t, 19.45, 20.05, 'XLSX', 'Drawing number', 'Atlas list · row 2,620', (700, 526),
             (INFO_X, 702))
    return base


@lru_cache(maxsize=1)
def row_lens():
    full = load('pdf/mt_p390.png', 'RGB')
    x0, y0, x1, y1 = MANIFEST['p390_row']
    crop = full.crop((x0 - 6, y0 - 6, x1 + 6, y1 + 6))
    crop = crop.resize((int(crop.width * 1.12), int(crop.height * 1.12)), Image.LANCZOS)
    spr, pad = card_sprite(crop.width + 24, crop.height + 24, 14, WHITE, 255, 1.2, (GOLD, 3))
    spr = spr.copy()
    put(spr, crop.convert('RGBA'), pad + 12, pad + 12)
    return spr


def p390_row_center():
    sc = PAGE_W / MANIFEST['p390_size'][0]
    r = [v * sc for v in MANIFEST['p390_row']]
    return PAGE_X + (r[0] + r[2]) / 2, PAGE_Y + (r[1] + r[3]) / 2


def chip_fly(base, t, t0, t1, kind, title, sub, src, dst, w=470, h=84):
    if t < t0:
        return
    u = ease_in_out(prog(t, t0, t1))
    sx, sy = src[0] - w / 2, src[1] - h / 2
    x = lerp(sx, dst[0], u)
    y = lerp(sy, dst[1], u) - 80 * math.sin(u * math.pi)
    sc = lerp(.7, 1.0, u)
    chip_at(base, kind, title, sub, x, y, smooth(prog(t, t0, t0 + .15)), sc, w, h)


SHEET = MANIFEST['atlas_strip']
SX, SY, SW, SH_ = 100, 110, SHEET['width'], 770
VIEW_Y0 = SY + 46 + 64
VIEW_H = SY + SH_ - 14 - VIEW_Y0
OFF_T = SHEET['target_y'] - (VIEW_H // 2 - SHEET['row_h'] // 2)


def sheet_offset(t):
    return OFF_T * ease_out(prog(t, 17.55, 19.0), 4)


def s3_sheet(base, t):
    a = smooth(prog(t, 17.3, 17.6))
    dx = 220 * (1 - ease_out(prog(t, 17.3, 17.75)))
    x = SX + dx
    card(base, x, SY, SW, SH_, 22, alpha=a)
    bar = rrect(SW, 70, 22, (33, 115, 70)).crop((0, 0, SW, 46))
    put(base, bar, x, SY, a)
    text(base, 'Atlas_Components_-_Mine_Train.xlsx', x + 24, SY + 10, 21, 'demi', WHITE, a)
    put(base, load('sheet/atlas_header.png'), x, SY + 46, a)
    strip = load('sheet/atlas_strip.png')
    off = sheet_offset(t)
    v = (sheet_offset(t + 1 / 60) - off) * 60
    crop = strip.crop((0, int(off), SW, int(off) + VIEW_H))
    if v > 1800:
        k = min(3, int(v / 1800) + 1)
        step = v / 60 / (k + 1)
        for j in range(1, k + 1):
            o2 = int(off - step * j)
            if o2 >= 0:
                crop = Image.blend(crop, strip.crop((0, o2, SW, o2 + VIEW_H)), 1 / (j + 1))
    put(base, crop, x, VIEW_Y0, a)
    ry = VIEW_Y0 + SHEET['target_y'] - off
    k = smooth(prog(t, 19.02, 19.3))
    if k > 0 and VIEW_Y0 <= ry <= VIEW_Y0 + VIEW_H - 34:
        rw = SW - SHEET['num_w']
        put(base, rrect(rw, 34, 4, GOLD, 80), x + SHEET['num_w'], ry, a * k)
        put(base, ring(rw, 34, 4, 3, GOLD), x + SHEET['num_w'], ry, a * k)
    # info column
    ia = smooth(prog(t, 17.45, 17.8))
    row = SHEET['first_row'] + int((off + VIEW_H / 2) / SHEET['row_h'])
    text(base, 'SPREADSHEET', INFO_X, 160, 19, 'demi', TEAL, ia, track=3)
    text(base, 'Atlas drawing list', INFO_X, 190, 36, 'demi', INK, ia)
    text(base, 'Every component, every drawing number', INFO_X, 244, 24, 'medium', INK2, ia)
    text(base, f'Row {row:,}', INFO_X, 290, 88, 'heavy', TEAL, ia)


# ================================================================== S4 · cross-check
Q_CFRAME = ["What's the C-frame drawing number at Mine Train,",
            "its NDT requirement, and any MRB records?"]
SOURCES = [('XLSX', 'Atlas spreadsheet'), ('PDF', 'Manual p.390'), ('SCAN', 'QA repair records')]
ROWS4 = [
    ('DRAWING', 'SDLDWARMV0217', 'C-Frame · Rev E', ('XLSX', 'Atlas_Components – Mine Train', 'row 2,620')),
    ('INSPECTION RULE', 'Crack test every 240,000 laps', 'Visual + magnetic particle or penetrant test',
     ('PDF', 'Mine Train manual', 'page 390')),
    ('REPAIR HISTORY', '3 MRB records · all closed', 'Jun 2024 · May 2025 · Nov 2025',
     ('SCAN', 'QAQC MRB Record', '3 matching rows')),
]
ROW_T = [24.2, 24.75, 25.3]
VIEW3D_T, VIEW3D_CLICK = 25.55, 26.2


def s4(t):
    base = bg_light().copy()
    header(base, t, 20.2, '01', 'VERSATILITY', 'Ask once. It cross-checks everything.')
    genie_card(base, t, 20.25, bounce=22.55)
    a = chat_shell(base, t, 20.3, tag='Cross-check')
    user_bubble(base, t, Q_CFRAME, 1770, 216, 20.45, 22.35, alpha=a)
    # cursor -> send
    if 21.9 <= t < 23.0:
        u = ease_in_out(prog(t, 21.95, 22.4))
        cx, cy = lerp(1480, 1752, u), lerp(700, 316, u)
        cursor(base, cx, cy, smooth(prog(t, 21.9, 22.05)) * (1 - smooth(prog(t, 22.75, 23.0))),
               click=(t - 22.5) if t >= 22.5 else None)
    # three sources
    lab_a = smooth(prog(t, 22.7, 22.95))
    agree = smooth(prog(t, 24.0, 24.25))
    if lab_a > 0:
        text(base, 'Cross-checking 3 sources…', 430, 352, 22, 'medium', MUTED, lab_a * (1 - agree))
        if agree > 0:
            put(base, icon('check', 26, GREEN), 430, 352, agree)
            text(base, '3 sources agree', 466, 350, 22, 'demi', GREEN, agree)
    x = 430
    for i, (kind, label) in enumerate(SOURCES):
        t0 = 22.8 + i * .22
        width = int(tw(label, 21, 'demi')) + 84
        if t >= t0:
            u = ease_out(prog(t, t0, t0 + .5))
            sx, sy = 1500 + i * 30, 640 + i * 70
            px, py = lerp(sx, x, u), lerp(sy, 394, u)
            act = 0.0
            for k, rt in enumerate(ROW_T):
                if k == i:
                    act = max(act, window(t, rt, rt + .9, .15, .4))
            source_pill(base, kind, label, px, py, smooth(prog(t, t0, t0 + .2)), glow_k=act)
            # progress → check
            p = prog(t, 23.1 + i * .1, 23.7 + i * .1)
            if 0 < p < 1:
                put(base, rrect(int((width - 20) * p) + 4, 4, 2, TEAL), px + 10, py + 56)
            if p >= 1:
                ca, cs = pop(t, 23.7 + i * .1, .3)
                putc(base, icon('check', 24, GREEN), px + width - 4, py + 4, ca, cs)
        x += width + 18
    # answer rows
    for k, (label, value, detail, src) in enumerate(ROWS4):
        t0 = ROW_T[k]
        ra = smooth(prog(t, t0, t0 + .3))
        if ra <= 0:
            continue
        y = 482 + k * 128
        dx = -24 * (1 - ease_out(prog(t, t0, t0 + .45)))
        put(base, rrect(6, 104, 3, TEAL), 430 + dx, y, ra)
        text(base, label, 458 + dx, y + 2, 18, 'demi', MUTED, ra, track=2)
        text(base, value, 458 + dx, y + 28, 38, 'demi', INK, ra)
        text(base, detail, 458 + dx, y + 80, 22, 'medium', INK2, ra)
        kind, title, sub = src
        cw = 400
        cx = 1770 - cw
        put(base, rrect(cw, 84, 18, (247, 250, 251)), cx - dx, y + 10, ra)
        put(base, ring(cw, 84, 18, 2, LINE), cx - dx, y + 10, ra)
        put(base, file_badge(kind, 44), cx - dx + 16, y + 30, ra)
        text(base, title, cx - dx + 74, y + 22, 20, 'demi', INK, ra)
        text(base, sub, cx - dx + 74, y + 52, 18, 'medium', TEAL, ra)
    va, vs = pop(t, 25.7, .35)
    if va > 0:
        pw = int(tw('Verified against 3 sources', 22, 'demi')) + 70
        spr = Image.new('RGBA', (pw, 48), (0, 0, 0, 0))
        put(spr, rrect(pw, 48, 24, GOLD_L), 0, 0)
        put(spr, ring(pw, 48, 24, 2, GOLD), 0, 0)
        put(spr, icon('check', 26, GOLD), 14, 11)
        text(spr, 'Verified against 3 sources', 50, 10, 22, 'demi', (130, 84, 0))
        putc(base, spr, 1770 - pw / 2, 386 + 24, va, vs)
    view3d_button(base, t)
    return base


def view3d_button(base, t):
    ba, bs = pop(t, VIEW3D_T, .35)
    if ba <= 0:
        return
    bx = 458 + tw(ROWS4[0][1], 38, 'demi') + 30
    by = 482 + 26
    hot = t >= VIEW3D_CLICK
    bw = int(tw('View in 3D', 21, 'demi')) + 92
    spr = Image.new('RGBA', (bw, 48), (0, 0, 0, 0))
    put(spr, rrect(bw, 48, 24, TEAL if hot else TEAL_L), 0, 0)
    put(spr, rrect(40, 30, 8, WHITE if hot else TEAL), 10, 9)
    text(spr, '3D', 30, 12, 17, 'heavy', TEAL if hot else WHITE, anchor='c')
    text(spr, 'View in 3D', 62, 10, 21, 'demi', WHITE if hot else TEAL_D)
    press = 1 - .05 * math.sin(clamp((t - VIEW3D_CLICK) / .2) * math.pi) if hot else 1
    putc(base, spr, bx + bw / 2, by + 24, ba, bs * press)
    if VIEW3D_T + .1 <= t:
        u = ease_in_out(prog(t, VIEW3D_T + .15, VIEW3D_CLICK - .05))
        cx, cy = lerp(bx + 520, bx + bw / 2 + 10, u), lerp(by + 300, by + 30, u)
        cursor(base, cx, cy, smooth(prog(t, VIEW3D_T + .1, VIEW3D_T + .25)),
               click=(t - VIEW3D_CLICK) if hot else None)


# ================================================================== S5 · tracker
TRACKER = [
    ('Soaring AA gearbox', 'GREEN', '60', '2'),
    ('Soaring AB gearbox', 'GREEN', '69', '1'),
    ('Mine Train lift gearbox', 'GREEN', '67', '3'),
    ('Carousel drive gearbox', 'AMBER', '52', '11'),
    ('Dumbo gearbox', 'AMBER', '53', '6'),
    ('Slinky Kicker Wheel #3', 'AMBER', '625', '23'),
    ('Slinky Kicker Wheel #6', 'AMBER', '1,310', '16'),
    ('Slinky Kicker Wheel #7', 'AMBER', '1,349', '56'),
    ('Slinky Kicker Wheel #11', 'AMBER', '1,007', '58'),
    ('Slinky Kicker Wheel #1', 'RED', '4,215', '51'),
]
LIGHT = {'GREEN': (GREEN, GREEN_L), 'AMBER': (AMBER, (255, 238, 210)), 'RED': (RED, RED_L)}
WALL_X, WALL_Y, TH_W, TH_H, GAP, COLS = 108, 168, 68, 96, 8, 9
TAB_X, TAB_Y, TAB_W, TAB_H = 850, 130, 970, 760
COLS5 = [(0, 'EQUIPMENT'), (320, 'SAMPLED'), (488, 'STATUS'), (626, 'WATER ppm'), (790, 'IRON ppm')]
ROW5_Y0, ROW5_H = TAB_Y + 160, 52


def thumb_pos(i):
    c, r = i % COLS, i // COLS
    return WALL_X + c * (TH_W + GAP), WALL_Y + r * (TH_H + GAP)


@lru_cache(maxsize=80)
def thumb(i):
    img = load(f'oil_thumbs/{i:02d}.jpg', 'RGB').resize((TH_W, TH_H), Image.LANCZOS).convert('RGBA')
    ImageDraw.Draw(img).rectangle((0, 0, TH_W - 1, TH_H - 1), outline=(200, 206, 214, 255))
    return img


SRC_FOR_ROW = [7, 11, 20, 30, 26, 41, 38, 44, 49, 36]


def s5(t):
    base = bg_light().copy()
    header(base, t, 27.9, '01', 'VERSATILITY', 'Scattered reports become one tracker')
    n = MANIFEST['oil_count']
    la = smooth(prog(t, 27.95, 28.3))
    text(base, 'Scanned lab reports', WALL_X, 128, 26, 'demi', INK, la)
    for i in range(n):
        t0 = 27.95 + i * .013
        if t < t0:
            continue
        u = ease_out(prog(t, t0, t0 + .55))
        x, y = thumb_pos(i)
        sx, sy = 420, 520
        hot = 0.0
        for k, src in enumerate(SRC_FOR_ROW):
            if src == i:
                hot = window(t, 29.0 + k * .2, 29.0 + k * .2 + .5, .08, .3)
        px, py = lerp(sx, x, u), lerp(sy, y, u)
        put(base, thumb(i), px, py, smooth(prog(t, t0, t0 + .15)) * (1 - .35 * smooth(prog(t, 31.2, 31.6))))
        if hot > 0:
            put(base, ring(TH_W + 8, TH_H + 8, 6, 3, TEAL), px - 4, py - 4, hot)
    # table
    ta = smooth(prog(t, 28.5, 28.85))
    dx = 40 * (1 - ease_out(prog(t, 28.5, 29.0)))
    card(base, TAB_X + dx, TAB_Y, TAB_W, TAB_H, 28, alpha=ta)
    X = TAB_X + dx + 40
    text(base, 'Oil Inspection Tracker', X, TAB_Y + 30, 34, 'demi', INK, ta)
    pill(base, 'Built automatically', TAB_X + dx + TAB_W - 40, TAB_Y + 34, 20, 'demi', TEAL_D, TEAL_L, ta,
         anchor='r')
    put(base, rrect(TAB_W - 80, 44, 10, (243, 246, 248)), X - 10, TAB_Y + 100, ta)
    for cx, label in COLS5:
        text(base, label, X + cx, TAB_Y + 111, 17, 'demi', MUTED, ta, track=2)
    for k, (name, light, water, iron) in enumerate(TRACKER):
        t0 = 29.05 + k * .2
        y = ROW5_Y0 + k * ROW5_H
        # value flying from the scan into the row
        u = prog(t, t0 - .25, t0 + .05)
        if 0 < u < 1:
            sx, sy = thumb_pos(SRC_FOR_ROW[k])
            fx = lerp(sx + TH_W / 2, X + 20, ease_in_out(u))
            fy = lerp(sy + TH_H / 2, y + 22, ease_in_out(u)) - 60 * math.sin(u * math.pi)
            putc(base, rrect(54, 22, 11, TEAL), fx, fy, .9)
        ra = smooth(prog(t, t0, t0 + .22))
        if ra <= 0:
            continue
        col, bg = LIGHT[light]
        if light == 'RED':
            pulse = .55 + .45 * math.sin((t - 31.3) * 6) if t > 31.3 else .5
            g, gp = soft_rect_glow(TAB_W - 60, ROW5_H - 6, 10, RED, 14, 200)
            put(base, g, X - 20 - gp, y - gp, ra * pulse * smooth(prog(t, 31.25, 31.5)))
            put(base, rrect(TAB_W - 60, ROW5_H - 6, 10, RED_L), X - 20, y, ra)
        elif k % 2 == 1:
            put(base, rrect(TAB_W - 60, ROW5_H - 6, 10, (249, 250, 251)), X - 20, y, ra)
        text(base, name, X, y + 10, 23, 'demi' if light == 'RED' else 'medium', INK, ra)
        text(base, '2025-09-16', X + COLS5[1][0], y + 11, 21, 'medium', INK2, ra)
        pw = pill(base, light, X + COLS5[2][0], y + 8, 17, 'bold', col, bg, ra, padx=12, h=32)
        text(base, water, X + COLS5[3][0], y + 9, 24, 'demi', RED if light == 'RED' else INK, ra)
        text(base, iron, X + COLS5[4][0], y + 10, 22, 'medium', INK2, ra)
    fa = smooth(prog(t, 31.35, 31.65))
    if fa > 0:
        y = ROW5_Y0 + len(TRACKER) * ROW5_H + 14
        text(base, 'Status lights read straight from each lab report', X, y + 6, 21, 'medium', MUTED, fa)
        ba, bs = pop(t, 31.45, .35)
        pw = int(tw('Needs attention', 22, 'demi')) + 40
        spr = Image.new('RGBA', (pw, 44), (0, 0, 0, 0))
        put(spr, rrect(pw, 44, 22, RED), 0, 0)
        text(spr, 'Needs attention', 20, 7, 22, 'demi', WHITE)
        putc(base, spr, TAB_X + TAB_W - 40 - pw / 2, y + 16, ba, bs)
    return base


# ================================================================== S6 · picture search
Q_TOP = ['Find the Mine Train and Tron layouts,',
         'and photos of the real trains.']
RESULTS = [
    ('img/mt_topview.jpg', 'Mine Train · top view', 'Manual · page 72'),
    ('img/tron_track.jpg', 'Tron · track layout', 'Manual · page 134'),
    ('img/photo_2.jpg', 'Mine Train · real train', 'Photo library'),
    ('img/photo_1.jpg', 'Tron · real train', 'Photo library'),
]
GRID_X, GRID_Y = 432, 372
TILE_W, TILE_H, TG = 202, 150, 16


@lru_cache(maxsize=40)
def fig_tile(i):
    rel = 'figs/' + MANIFEST['figs'][i % len(MANIFEST['figs'])]
    return rounded_img(rel, TILE_W, TILE_H, 12, 'contain', (246, 248, 250))


@lru_cache(maxsize=40)
def fig_dots(i):
    src = fit('figs/' + MANIFEST['figs'][i % len(MANIFEST['figs'])], TILE_W, TILE_H, 'contain',
              (255, 255, 255)).convert('L')
    cols, rows = TILE_W // 8, TILE_H // 8
    small = src.resize((cols, rows), Image.BILINEAR)
    S = 3
    m = Image.new('L', (TILE_W * S, TILE_H * S), 0)
    d = ImageDraw.Draw(m)
    px = small.load()
    for r in range(rows):
        for c in range(cols):
            v = 1 - px[c, r] / 255
            rad = (0.7 + 2.6 * v) * S
            cx, cy = (c * 8 + 4) * S, (r * 8 + 4) * S
            d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=int(110 + 145 * v))
    m = m.resize((TILE_W, TILE_H), Image.LANCZOS)
    out = Image.new('RGBA', (TILE_W, TILE_H), (0, 0, 0, 0))
    out.alpha_composite(rrect(TILE_W, TILE_H, 12, (238, 248, 248)))
    out.alpha_composite(colorize(m, TEAL))
    return out


def s6(t):
    base = bg_light().copy()
    header(base, t, 32.6, '01', 'VERSATILITY', 'It even searches pictures')
    genie_card(base, t, 32.65, bounce=34.4)
    a = chat_shell(base, t, 32.7, tag='Picture search', tag_colors=((236, 230, 252), (96, 60, 180)))
    user_bubble(base, t, Q_TOP, 1770, 216, 32.85, 34.3, alpha=a)
    # dotted search over the image library
    if 34.3 <= t < 35.9:
        ga = smooth(prog(t, 34.3, 34.5)) * (1 - smooth(prog(t, 35.45, 35.85)))
        beam = lerp(GRID_X - 60, GRID_X + 6 * (TILE_W + TG) + 60, ease_in_out(prog(t, 34.45, 35.35)))
        for i in range(18):
            c, r = i % 6, i // 6
            x, y = GRID_X + c * (TILE_W + TG), GRID_Y + r * (TILE_H + TG)
            ta = ga * smooth(prog(t, 34.3 + i * .012, 34.5 + i * .012))
            if beam > x + TILE_W:
                put(base, fig_dots(i), x, y, ta)
            elif beam > x:
                cut = int(beam - x)
                put(base, fig_dots(i).crop((0, 0, cut, TILE_H)), x, y, ta)
                put(base, fig_tile(i).crop((cut, 0, TILE_W, TILE_H)), x + cut, y, ta)
            else:
                put(base, fig_tile(i), x, y, ta)
        if 34.45 < t < 35.4:
            put(base, rrect(6, 3 * (TILE_H + TG) + 20, 3, TEAL), beam - 3, GRID_Y - 10, ga)
            g = glow(260, TEAL, 90)
            putc(base, g.resize((120, 3 * (TILE_H + TG) + 60)), beam, GRID_Y + 240, ga * .8)
        text(base, 'Reading every figure and photo as searchable patterns…', GRID_X, 332, 22, 'medium',
             MUTED, ga)
    # results 2x2
    rw, rh, gap = 655, 248, 26
    for i, (rel, title, sub) in enumerate(RESULTS):
        t0 = 35.45 + i * .16
        if t < t0:
            continue
        ra, rs = pop(t, t0, .45)
        c, r = i % 2, i // 2
        x, y = 432 + c * (rw + gap), 330 + r * (rh + 24)
        spr = _result_sprite(i, rel, title, sub, rw, rh)
        putc(base, spr, x + rw / 2, y + rh / 2, ra, rs)
    return base


@lru_cache(maxsize=8)
def _result_sprite(i, rel, title, sub, rw, rh):
    spr, pad = card_sprite(rw, rh, 22, WHITE, 255, .8, (LINE, 2))
    spr = spr.copy()
    put(spr, rounded_img(rel, rw - 24, rh - 74, 14, 'contain' if i < 2 else 'cover', (245, 247, 250)),
        pad + 12, pad + 12)
    put(spr, icon('check', 26, GREEN), pad + 16, pad + rh - 48)
    text(spr, title, pad + 52, pad + rh - 52, 23, 'demi', INK)
    text(spr, sub, pad + rw - 20, pad + rh - 50, 19, 'medium', TEAL, anchor='r')
    return spr
