"""Scenes 7-12: automation (just ask, Teams alert, weekly email), every team, close."""
from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image

from common import *  # noqa: F401,F403
from scenes_a import chat_shell, header, user_bubble

OUTLOOK = (0, 114, 198)


def gen_avatar(base, x, y, d=40, alpha=1.0):
    put(base, circle(d, TEAL_L), x, y, alpha)
    putc(base, genie('front', int(d * .8)), x + d / 2, y + d / 2, alpha)


@lru_cache(maxsize=32)
def initials(s, d, color):
    spr = Image.new('RGBA', (d, d), (0, 0, 0, 0))
    put(spr, circle(d, color), 0, 0)
    text(spr, s, d / 2, d * .2, int(d * .38), 'demi', WHITE, anchor='c')
    return spr


def signature(base, x, y, alpha=1.0, k=1.0):
    put(base, solid(int(560 * k), 2, LINE), x, y, alpha)
    text(base, 'Powered by', x, y + 20, int(18 * k), 'medium', MUTED, alpha)
    wm = load('brand/engenie-email-wordmark.png')
    wm = xform(wm, .62 * k, fast=False)
    put(base, wm, x + int(108 * k), y + 10, alpha)
    text(base, 'Intelligent AI Agent built by FOS / Engineering the Magic!', x, y + int(64 * k), int(17 * k),
         'medium', MUTED, alpha)


# ================================================================== S7 · just ask
CODE = [
    (['GET /api/v2/attractions/wait-times', 'Authorization: Bearer ••••••••'], 160, 210, -5),
    (['cron:  */30 8-22 * * *'], 1240, 170, 4),
    (['def parse(page):', "    rows = soup.select('td')", '    return clean(rows)'], 1330, 640, -3),
    (['{ "ride": "TRON", "train": "03",', '  "meter": … }'], 120, 640, 3),
    (['cron:  0 13 * * *'], 760, 800, -2),
    (['SMTP_HOST = …', 'SMTP_PORT = 587'], 1460, 420, 6),
    (['POST https://…/webhook/teams'], 260, 430, -6),
    (['pip install requests pandas'], 840, 120, 2),
]


@lru_cache(maxsize=16)
def code_card(i):
    lines = CODE[i][0]
    w = int(max(tw(l, 22, 'mono') for l in lines)) + 52
    h = 34 * len(lines) + 34
    spr, pad = card_sprite(w, h, 16, (22, 30, 46), 255, 1.2)
    spr = spr.copy()
    for k, color in enumerate([(255, 96, 92), (255, 190, 60), (60, 200, 90)]):
        put(spr, circle(10, color), pad + 14 + k * 16, pad + 10)
    for k, l in enumerate(lines):
        text(spr, l, pad + 24, pad + 26 + k * 34, 22, 'mono', (170, 226, 200))
    return spr


COSTS = [('Engineers', 'person', 55.15), ('Code', 'doc', 56.02), ('Setup', 'clock', 56.55)]
BIG_WORDS = [('No code.', 57.8), ('No setup.', 58.35)]
S7_T0, WITH_GENIE_T, JUST_ASK_T, BUILDER_T = 53.4, 57.05, 59.1, 59.85
CODE_FADE = (58.0, 58.95)

BUILDER = (110, 110, 1000, 790)
MSGS = [
    ('user', ['Every day, tell my team in Teams which trains', 'are out of service or on standby.'], 60.35, 64.66),
    ('ok', ['Lightboard connected', 'Every day', 'Teams alert on'], 65.15, None),
    ('user', ['Every week, email the ride downtime report', 'to Mickey, Minnie, Donald and Goofy.'], 68.4, 72.71),
    ('report', None, 73.2, None),
]
REPORT_CLICK = 73.8
TEAMS_WIN = (1150, 110, 660, 790)
TEAMS_T = 65.45
# lightboard snapshot 2026-11-14 15:30: red light = out of service, green light with 1 lap = standby
ALERT = [('OUT OF SERVICE', RED, ['Seven Dwarfs Mine Train · Train 01', 'TRON Lightcycle Power Run · Train 03']),
         ('ON STANDBY', AMBER, ['Seven Dwarfs Mine Train · Train 03', 'TRON Lightcycle Power Run · Train 08'])]


@lru_cache(maxsize=4)
def cost_pill(i):
    label, ic, _ = COSTS[i]
    w = int(tw(label, 40, 'demi')) + 130
    spr, pad = card_sprite(w, 92, 46)
    spr = spr.copy()
    put(spr, circle(60, (236, 239, 243)), pad + 16, pad + 16)
    putc(spr, icon(ic, 32, INK2), pad + 46, pad + 46)
    text(spr, label, pad + 96, pad + 22, 40, 'demi', INK)
    return spr


def msg_height(kind, lines):
    if kind == 'user':
        return int(26 * 1.45) * len(lines) + 30
    if kind == 'ok':
        return 50
    return 268


def s7(t):
    base = bg_light().copy()
    header(base, t, S7_T0 + .05, '02', 'AUTOMATION', 'Live data and reports, on autopilot')
    # ---- what it normally takes
    if t < BUILDER_T + .55:
        gone = smooth(prog(t, *CODE_FADE))
        for i, (lines, x, y, rot) in enumerate(CODE):
            a, sc = pop(t, 53.7 + i * .3, .35)
            spr = code_card(i)
            cx = x + spr.width / 2
            if gone >= 1:
                continue
            putc(base, spr, cx, y + spr.height / 2 - 12 * gone, a * (1 - gone), sc, rot)
        out_c = smooth(prog(t, 57.0, 57.3))
        if out_c < 1:
            text(base, 'Normally, it takes', 960, 392, 30, 'demi', MUTED,
                 smooth(prog(t, 54.0, 54.3)) * (1 - out_c), anchor='c')
            sprs = [cost_pill(i) for i in range(len(COSTS))]
            cx = 960 - (sum(s.width for s in sprs) + 30 * (len(sprs) - 1)) / 2
            for (label, ic, t0), spr in zip(COSTS, sprs):
                a, sc = pop(t, t0 - .1, .4)
                putc(base, spr, cx + spr.width / 2, 510, a * (1 - out_c), sc)
                cx += spr.width + 30
        # Keep the original centered words, then reveal the genie and Just ask below them.
        hold = 1 - smooth(prog(t, BUILDER_T, BUILDER_T + .55))
        intro = smooth(prog(t, WITH_GENIE_T, WITH_GENIE_T + .55))
        if intro > 0 and hold > 0:
            text(base, 'With En-Genie', 960, 280, 36, 'demi', INK2, intro * hold, anchor='c')
        for k, (word, t0) in enumerate(BIG_WORDS):
            a = smooth(prog(t, t0, t0 + .45))
            if a > 0 and hold > 0:
                putc(base, txt(word, 104, 'heavy', INK), 960, 410 + k * 130, a * hold,
                     lerp(.98, 1.0, a))
        ja = smooth(prog(t, JUST_ASK_T, JUST_ASK_T + .5))
        jo = smooth(prog(t, BUILDER_T, BUILDER_T + .55))
        if ja > 0 and jo < 1:
            scale = lerp(.98, 1.0, ja)
            putc(base, genie('look-up', 250), 640, 750 + 12 * (1 - ja), ja * (1 - jo), scale)
            putc(base, txt('Just ask.', 130, 'heavy', TEAL), 1090, 750 + 12 * (1 - ja), ja * (1 - jo), scale)
    # ---- the automated data engine + the team's Teams channel
    if t >= BUILDER_T:
        s7_builder(base, t)
        teams_panel(base, t)
    return base


def s7_builder(base, t):
    x, y, w, h = BUILDER
    a = smooth(prog(t, BUILDER_T, BUILDER_T + .5))
    dy = 24 * (1 - ease_out(prog(t, BUILDER_T, BUILDER_T + .55)))
    card(base, x, y + dy, w, h, 32, alpha=a)
    put(base, circle(52, TEAL_L), x + 34, y + 24 + dy, a)
    putc(base, icon('db', 28, TEAL), x + 60, y + 50 + dy, a)
    text(base, 'Automated data engine', x + 100, y + 20 + dy, 28, 'demi', INK, a)
    text(base, 'Tell En-Genie what you need, in plain words', x + 100, y + 56 + dy, 20, 'medium', MUTED, a)
    put(base, solid(w - 72, 2, LINE), x + 36, y + 96 + dy, a)
    top, bottom = y + 112, y + h - 18
    layer = Image.new('RGBA', (w, bottom - top), (0, 0, 0, 0))
    cy = 14
    for kind, payload, t0, t1 in MSGS:
        mh = msg_height(kind, payload)
        if t >= t0 - .3:
            if kind == 'user':
                user_bubble(layer, t, payload, w - 40, cy, t0, t1, size=26)
            elif kind == 'ok':
                ga = smooth(prog(t, t0, t0 + .2))
                gen_avatar(layer, 40, cy + 4, 42, ga)
                cx = 98
                for k, label in enumerate(payload):
                    pa, ps = pop(t, t0 + k * .15, .3)
                    pw = int(tw(label, 21, 'demi')) + 62
                    spr = Image.new('RGBA', (pw, 44), (0, 0, 0, 0))
                    put(spr, rrect(pw, 44, 22, GREEN_L), 0, 0)
                    put(spr, icon('check', 24, GREEN), 12, 10)
                    text(spr, label, 46, 8, 21, 'demi', (20, 110, 60))
                    putc(layer, spr, cx + pw / 2, cy + 25, pa, ps)
                    cx += pw + 12
            else:
                report_card(layer, t, 40, cy, t0)
        cy += mh + 18
    put(base, layer, x, top + dy, a)


def teams_panel(base, t):
    tx, ty, tw_, th = TEAMS_WIN
    a = smooth(prog(t, BUILDER_T + .1, BUILDER_T + .6))
    dx = 24 * (1 - ease_out(prog(t, BUILDER_T + .1, BUILDER_T + .65)))
    tx += dx
    card(base, tx, ty, tw_, th, 26, alpha=a)
    put(base, rrect(tw_, 90, 26, PURPLE).crop((0, 0, tw_, 64)), tx, ty, a)
    put(base, rrect(40, 40, 10, WHITE), tx + 22, ty + 12, a)
    text(base, 'T', tx + 42, ty + 14, 26, 'heavy', PURPLE, a, anchor='c')
    text(base, 'Teams', tx + 76, ty + 16, 22, 'demi', WHITE, a)
    text(base, 'FOS · Ride status', tx + tw_ - 30, ty + 20, 19, 'medium', (226, 228, 255), a, anchor='r')
    for k in range(2):
        put(base, circle(40, (226, 228, 236)), tx + 32, ty + 96 + k * 72, a)
        put(base, rrect(300 - k * 80, 14, 7, (236, 238, 242)), tx + 90, ty + 102 + k * 72, a)
        put(base, rrect(200 - k * 40, 14, 7, (242, 243, 246)), tx + 90, ty + 124 + k * 72, a)
    ca = smooth(prog(t, TEAMS_T, TEAMS_T + .25))
    if ca <= 0:
        return
    cy = ty + 236 + 60 * (1 - ease_out(prog(t, TEAMS_T, TEAMS_T + .4)))
    gen_avatar(base, tx + 30, cy, 48, ca)
    text(base, 'En-Genie', tx + 92, cy - 2, 21, 'demi', INK, ca)
    text(base, 'Sat 11/14 · 15:30', tx + 196, cy + 1, 18, 'medium', MUTED, ca)
    bx, bw, bh = tx + 92, tw_ - 122, 372
    by = cy + 36
    pulse = .5 + .5 * math.sin((t - TEAMS_T) * 6) if t > TEAMS_T + .3 else 0
    g, gp = soft_rect_glow(bw, bh, 18, RED, 16, 200)
    put(base, g, bx - gp, by - gp, ca * pulse * .5 * (1 - smooth(prog(t, TEAMS_T + 1.6, TEAMS_T + 2.4))))
    put(base, rrect(bw, bh, 18, WHITE), bx, by, ca)
    put(base, ring(bw, bh, 18, 2, LINE), bx, by, ca)
    put(base, rrect(8, bh - 36, 4, RED), bx + 16, by + 18, ca)
    text(base, 'Train status · Lightboard', bx + 42, by + 22, 26, 'demi', INK, ca)
    text(base, 'Trains out of service or on standby today', bx + 42, by + 60, 18, 'medium', MUTED, ca)
    yy = by + 104
    for k, (label, col, rows) in enumerate(ALERT):
        sa = smooth(prog(t, TEAMS_T + .25 + k * .25, TEAMS_T + .5 + k * .25))
        put(base, circle(16, col), bx + 44, yy + 5, ca * sa)
        text(base, label, bx + 70, yy, 17, 'demi', col, ca * sa, track=2)
        for j, row in enumerate(rows):
            text(base, row, bx + 70, yy + 30 + j * 34, 21, 'demi', INK, ca * sa)
        yy += 30 + 34 * len(rows) + 22
    na, ns = pop(t, TEAMS_T + .05, .3)
    putc(base, circle(30, RED), tx + 170, ty + 22, a * na, ns)


def report_card(base, t, x, y, t0):
    a = smooth(prog(t, t0, t0 + .25))
    if a <= 0:
        return
    gen_avatar(base, x, y + 4, 42, a)
    X = x + 58
    cw, ch = 820, 262
    card(base, X, y, cw, ch, 22, alpha=a, shadow_k=.6, border=(LINE, 2))
    text(base, 'Weekly downtime report', X + 28, y + 20, 27, 'demi', INK, a)
    pill(base, 'Ready to schedule', X + cw - 24, y + 22, 18, 'demi', TEAL_D, TEAL_L, a, anchor='r')
    rows = [('mail', 'Outlook email · every week'), ('person', 'To: Mickey · Minnie · Donald · Goofy'),
            ('doc', 'En-Genie summary + charts attached')]
    for k, (ic, label) in enumerate(rows):
        ra = smooth(prog(t, t0 + .1 + k * .1, t0 + .3 + k * .1))
        put(base, icon(ic, 26, TEAL), X + 30, y + 76 + k * 40, a * ra)
        text(base, label, X + 70, y + 72 + k * 40, 22, 'medium', INK2, a * ra)
    done = t >= REPORT_CLICK + .04
    label = 'Report schedule enabled' if done else 'Confirm and enable report schedule'
    bw = int(tw(label, 22, 'demi')) + (84 if done else 48)
    color = GREEN if done else TEAL
    bx, by = X + 28, y + ch - 66
    ba = smooth(prog(t, t0 + .3, t0 + .5)) * a
    c = REPORT_CLICK
    sc = 1 - .05 * math.sin(clamp((t - c) / .2) * math.pi) if c < t < c + .2 else 1
    spr = Image.new('RGBA', (bw, 48), (0, 0, 0, 0))
    put(spr, rrect(bw, 48, 24, color), 0, 0)
    if done:
        put(spr, icon('check', 26, WHITE), 18, 11)
    text(spr, label, 54 if done else 24, 9, 22, 'demi', WHITE)
    putc(base, spr, bx + bw / 2, by + 24, ba, sc)
    if c - .45 <= t < c + .6:
        u = ease_in_out(prog(t, c - .4, c - .06))
        cx, cy = lerp(bx + 720, bx + 200, u), lerp(by + 170, by + 30, u)
        cursor(base, cx, cy, smooth(prog(t, c - .45, c - .3)) * (1 - smooth(prog(t, c + .4, c + .6))),
               click=(t - c) if t >= c else None)


# ================================================================== S8 · weekly email, to everyone
S8_T0 = 74.2
EM = (150, 136, 900, 748)
BODY = [
    ('Weekly downtime report 11/01/2026 - 11/07/2026', 24, 'demi', INK),
    ('Maintenance only, same basis as the report header.', 22, 'medium', INK2),
    ('101: 11 events / 6.39 h', 25, 'demi', INK),
    ('105: 12 events / 20.93 h', 25, 'demi', INK),
    ('The three HTML reports are attached - open them in a browser.', 22, 'medium', INK2),
]
ATTACH = ['Downtime_Chart…', 'Weekly_Downtime_R…', 'Weekly_Downtime_R…']
RECIPIENTS = [('MM', 'Mickey Mouse'), ('MM', 'Minnie Mouse'), ('DD', 'Donald Duck'), ('G', 'Goofy')]
AV_COLORS = [(214, 102, 76), (196, 78, 140), (76, 140, 214), (46, 160, 120)]
FLY_T = 75.55


def avatar_pos(i):
    ang = math.radians(-39 + i * 26)
    return 1150 + 500 * math.cos(ang), 512 + 500 * math.sin(ang) * .8


def s8(t):
    base = bg_light().copy()
    header(base, t, S8_T0, '02', 'AUTOMATION', "Every week, in everyone's inbox")
    x, y, w, h = EM
    a = smooth(prog(t, S8_T0 + .05, S8_T0 + .4))
    y += 30 * (1 - ease_out(prog(t, S8_T0 + .05, S8_T0 + .55)))
    card(base, x, y, w, h, 28, alpha=a)
    put(base, rrect(w, 60, 28, OUTLOOK).crop((0, 0, w, 10)), x, y, a)
    X = x + 44
    text(base, 'Weekly downtime report - 11/01/2026 to 11/07/2026', X, y + 34, 29, 'demi', INK, a)
    put(base, initials('DD', 50, (92, 110, 200)), X, y + 96, a)
    text(base, 'Duck, Daisy', X + 64, y + 94, 22, 'demi', INK, a)
    pill(base, 'sent by En-Genie', X + 200, y + 94, 17, 'demi', TEAL_D, TEAL_L, a, h=30)
    text(base, 'To: Mouse, Mickey;  Mouse, Minnie;  Duck, Donald;  Goofy', X + 64, y + 128, 17, 'medium',
         MUTED, a)
    put(base, solid(w - 88, 2, LINE), X, y + 176, a)
    ax = X
    for k, name in enumerate(ATTACH):
        ka = smooth(prog(t, S8_T0 + .3 + k * .08, S8_T0 + .55 + k * .08))
        cw = 256
        put(base, rrect(cw, 54, 12, (247, 248, 250)), ax, y + 196, a * ka)
        put(base, ring(cw, 54, 12, 2, LINE), ax, y + 196, a * ka)
        put(base, file_badge('HTML', 36), ax + 10, y + 205, a * ka)
        text(base, name, ax + 56, y + 210, 18, 'demi', INK, a * ka)
        ax += cw + 12
    by = y + 286
    sum_y = by + 104
    hl_t = S8_T0 + .8
    hl = smooth(prog(t, hl_t, hl_t + .3))
    if hl > 0:
        put(base, rrect(int(340 * ease_out(prog(t, hl_t, hl_t + .4))), 96, 12, GOLD_L), X - 14, sum_y - 6, hl)
        put(base, rrect(6, 96, 3, GOLD), X - 14, sum_y - 6, hl)
        ta, ts = pop(t, hl_t + .15, .35)
        tag = Image.new('RGBA', (240, 40), (0, 0, 0, 0))
        put(tag, rrect(240, 40, 20, GOLD), 0, 0)
        put(tag, icon('spark', 22, WHITE), 14, 9)
        text(tag, 'En-Genie summary', 44, 6, 19, 'demi', WHITE)
        putc(base, tag, X + 470, sum_y + 42, ta, ts)
    yy = by
    for k, (s, size, wt, col) in enumerate(BODY):
        la = smooth(prog(t, S8_T0 + .25 + k * .1, S8_T0 + .5 + k * .1))
        if k == 2:
            yy = sum_y
        text(base, s, X, yy, size, wt, col, a * la)
        yy += 48 if k in (2, 3) else 44
    signature(base, X, yy + 22, a * smooth(prog(t, S8_T0 + .7, S8_T0 + 1.0)))
    # the same email lands with everyone
    sx, sy = x + w - 10, y + 360
    for i, (ini, name) in enumerate(RECIPIENTS):
        px, py = avatar_pos(i)
        aa, asc = pop(t, S8_T0 + .4 + i * .08, .35)
        t0 = FLY_T + i * .16
        u = prog(t, t0, t0 + .55)
        arrived = u >= 1
        if 0 < u < 1:
            e = ease_in_out(u)
            fx, fy = lerp(sx, px - 70, e), lerp(sy, py, e) - 50 * math.sin(e * math.pi)
            put(base, circle(40, OUTLOOK), fx - 20, fy - 20)
            putc(base, icon('mail', 22, WHITE), fx, fy)
        d = 88
        if arrived:
            g = window(t, t0 + .55, t0 + 1.05, .05, .4)
            put(base, circle(d + 16, GREEN, 90), px - d / 2 - 8, py - d / 2 - 8, g)
        putc(base, initials(ini, d, AV_COLORS[i]), px, py, aa, asc)
        text(base, name, px + d / 2 + 14, py - 14, 20, 'demi', INK, aa)
        if arrived:
            ca, cs = pop(t, t0 + .55, .3)
            put(base, circle(34, WHITE), px + d / 2 - 28, py + d / 2 - 30, ca)
            putc(base, icon('check', 28, GREEN), px + d / 2 - 11, py + d / 2 - 13, ca, cs)
    return base


# ================================================================== S11 · every team
NODES = {
    'manual': ('Maintenance|Manuals', 1060, 480, 104),
    'atlas': ('Atlas|Drawings', 800, 300, 70),
    'std': ('Disney Ride|Standard', 1090, 236, 72),
    'vekoma': ('Vekoma|TS', 1336, 300, 62),
    'meters': ('Ride|Meters', 790, 660, 64),
    'down': ('Downtime|Events', 1320, 618, 72),
    'mrb': ('QAQC MRB|Records', 1060, 760, 68),
    'rse': ('RSE Weekly|Report', 1556, 770, 64),
    'weather': ('Hourly|Weather', 1548, 456, 58),
    'optime': ('Daily|Op. Time', 1730, 300, 62),
    'wait': ('Wait|Time', 1740, 590, 60),
}
EDGES = [('weather', 'optime'), ('weather', 'wait'), ('atlas', 'manual'), ('rse', 'down'), ('rse', 'mrb'),
         ('optime', 'wait'), ('std', 'vekoma'), ('std', 'manual'), ('meters', 'manual'), ('vekoma', 'manual'),
         ('manual', 'down'), ('manual', 'mrb'), ('down', 'mrb')]
TEAMS = [('FOS', 250, 300, ['atlas', 'manual']), ('RSE', 250, 510, ['manual', 'meters']),
         ('QAQC', 250, 720, ['mrb'])]


@lru_cache(maxsize=16)
def node_sprite(key):
    label, _, _, r = NODES[key]
    d = r * 2
    spr = Image.new('RGBA', (d + 40, d + 40), (0, 0, 0, 0))
    sh, pad = shadow(d, d, r, 40, 14)
    put(spr, sh.resize((d + 40, d + 40)), 0, 6)
    put(spr, circle(d, WHITE), 20, 20)
    put(spr, ring(d, d, r, 4 if key == 'manual' else 3, TEAL), 20, 20)
    lines = label.split('|')
    size = 22 if key == 'manual' else 18
    lh = size + 6
    y0 = 20 + r - lh * len(lines) / 2 - 2
    for k, l in enumerate(lines):
        text(spr, l, 20 + r, y0 + k * lh, size, 'demi', INK, anchor='c')
    return spr


@lru_cache(maxsize=4)
def team_sprite(name):
    w, h = 190, 150
    spr, pad = card_sprite(w, h, 24)
    spr = spr.copy()
    for k, col in enumerate([(76, 140, 214), (46, 160, 120), (220, 150, 40)]):
        put(spr, circle(56, WHITE), pad + 32 + k * 40 - 3, pad + 22 - 3)
        put(spr, circle(50, col), pad + 32 + k * 40, pad + 22)
        putc(spr, icon('person', 30, WHITE), pad + 57 + k * 40, pad + 49)
    text(spr, f'{name} team', pad + w / 2, pad + 92, 24, 'demi', INK, anchor='c')
    return spr, pad


def s11(t):
    base = bg_light().copy()
    header(base, t, 62.8, '03', 'SCALABILITY', 'Built for every team')
    keys = list(NODES)
    # library links
    for k, (p, q) in enumerate(EDGES):
        u = prog(t, 63.3 + k * .07, 63.75 + k * .07)
        if u > 0:
            a_, b_ = NODES[p], NODES[q]
            line(base, [(a_[1], a_[2]), (b_[1], b_[2])], (120, 196, 200), 3, .9, ease_out(u))
    # team access lines + flowing dots
    for i, (name, x, y, links) in enumerate(TEAMS):
        for j, key in enumerate(links):
            n = NODES[key]
            u = prog(t, 64.5 + i * .15 + j * .1, 65.0 + i * .15 + j * .1)
            if u <= 0:
                continue
            p0, p3 = (x + 96, y), (n[1] - n[3], n[2])
            pts = bezier(p0, (p0[0] + 160, p0[1]), (p3[0] - 160, p3[1]), p3, 30)
            line(base, pts, GOLD, 3, .85, ease_out(u))
            if u >= 1:
                for m in range(2):
                    v = ((t - 65.0) * .7 + m * .5 + i * .2 + j * .1) % 1
                    pt = pts[int(v * (len(pts) - 1))]
                    putc(base, circle(12, GOLD), pt[0], pt[1])
    for k, key in enumerate(keys):
        a, sc = pop(t, 62.85 + k * .06, .4)
        _, x, y, r = NODES[key]
        putc(base, node_sprite(key), x, y, a, sc)
    for i, (name, x, y, _) in enumerate(TEAMS):
        a, sc = pop(t, 64.25 + i * .12, .4)
        spr, pad = team_sprite(name)
        putc(base, spr, x, y, a, sc)
    # personal library
    pa, ps = pop(t, 65.7, .4)
    if pa > 0:
        px, py = 560, 830
        spr = Image.new('RGBA', (340, 92), (0, 0, 0, 0))
        put(spr, rrect(340, 92, 46, WHITE), 0, 0)
        put(spr, ring(340, 92, 46, 3, (180, 186, 196)), 0, 0)
        put(spr, circle(60, (236, 238, 242)), 16, 16)
        putc(spr, icon('lock', 30, INK2), 46, 46)
        text(spr, 'KJ Spare Plan', 92, 14, 22, 'demi', INK)
        text(spr, 'Personal library · only me', 92, 48, 18, 'medium', MUTED)
        putc(base, spr, px, py, pa, ps)
    sa, ss = pop(t, 65.3, .4)
    if sa > 0:
        spr = Image.new('RGBA', (330, 46), (0, 0, 0, 0))
        put(spr, rrect(330, 46, 23, GOLD_L), 0, 0)
        put(spr, icon('link', 26, GOLD), 16, 10)
        text(spr, 'Shared libraries, linked', 52, 8, 21, 'demi', (130, 84, 0))
        putc(base, spr, 1330, 150, sa, ss)
    return base


# ================================================================== S12 · close
TILES = [('Any file', 'doc'), ('Cross-check', 'check'), ('Picture search', 'image'),
         ('Just ask', 'send'), ('Outlook & Teams', 'mail'), ('Every team', 'person')]
TILE_VIS = ['pdf/mt_cover.jpg', None, 'img/photo_2.jpg', None, None, None]


@lru_cache(maxsize=8)
def tile_sprite(i):
    label, ic = TILES[i]
    w, h = 300, 196
    spr, pad = card_sprite(w, h, 26)
    spr = spr.copy()
    if TILE_VIS[i]:
        put(spr, rounded_img(TILE_VIS[i], w - 32, 110, 14, 'cover'), pad + 16, pad + 16)
    else:
        put(spr, rrect(w - 32, 110, 14, TEAL_L), pad + 16, pad + 16)
        putc(spr, icon(ic, 58, TEAL), pad + w / 2, pad + 71)
    text(spr, label, pad + w / 2, pad + 140, 26, 'demi', INK, anchor='c')
    return spr


def s12(t):
    base = bg_light().copy()
    cx, cy = 960, 420
    conv = ease_in(prog(t, 70.2, 70.9), 2.2)
    for i in range(len(TILES)):
        a, sc = pop(t, 67.65 + i * .08, .4)
        ang = -math.pi / 2 + i * math.tau / 6 + .12 * (t - 67.6) + conv * 2.2
        r = 1 - conv
        x, y = cx + math.cos(ang) * 620 * r, cy + math.sin(ang) * 250 * r
        putc(base, tile_sprite(i), x, y, a * (1 - conv ** 2), sc * (1 - .8 * conv))
    ga, gs = pop(t, 67.7, .5)
    burst = smooth(prog(t, 70.75, 70.95))
    putc(base, glow(700, (120, 210, 220), 160), cx, cy, ga * (.5 + .5 * conv))
    putc(base, genie('genie', 300), cx, cy, ga, gs * (1 + .12 * conv))
    if burst > 0:
        for k in range(10):
            ang = k * math.tau / 10
            d = 150 + 220 * burst
            putc(base, icon('spark', 34, GOLD), cx + math.cos(ang) * d, cy + math.sin(ang) * d, 1 - burst * .6)
    # tagline, timed to the narration
    x = 960
    words = [('Every file.', 67.9, INK), ('Every team.', 68.81, INK), ('Always on.', 69.59, TEAL)]
    widths = [txt(w_, 64, 'heavy', c).width for w_, _, c in words]
    total = sum(widths) + 40 * 2
    x0 = 960 - total / 2
    for (w_, t0, c), wd in zip(words, widths):
        a, sc = pop(t, t0, .35)
        putc(base, txt(w_, 64, 'heavy', c), x0 + wd / 2, 905, a * (1 - .7 * conv), sc)
        x0 += wd + 40
    return base
