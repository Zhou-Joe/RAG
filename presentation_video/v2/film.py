"""EnGenie V2 · 85 s film. Timeline, transitions, captions and the render driver.

  v2_env/bin/python v2/film.py preview 1.0 8.6 ...   # still frames -> v2/preview/
  v2_env/bin/python v2/film.py sheet [step]          # contact sheet of the whole film
  v2_env/bin/python v2/film.py render                # full 1080p60 video (silent) -> v2/out/
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from functools import lru_cache
from multiprocessing import Pool

from PIL import Image

from common import *  # noqa: F401,F403
from common import HERE
import scenes_a as A
import scenes_b as B
import scenes_c as C
from scenes_c import old_frame

DURATION = 85.0
FFMPEG = '/opt/homebrew/bin/ffmpeg'
OUT = HERE / 'out'
PREVIEW = HERE / 'preview'

VO_START = {
    'archive': 0.30, 'q_wow': 6.25, 'proof': 10.35, 'brand': 13.0,
    'anyfile': 19.14, 'manual': 20.47, 'sheet': 23.13, 'q_cross': 25.85, 'cross': 33.15, 'part3d': 37.1,
    'tracker': 41.05, 'q_pics': 45.8, 'pictures': 51.55,
    'bridge': 53.6, 'justask': 57.15, 'q_teams': 60.35, 'teams': 65.6, 'q_report': 68.4, 'weekly': 74.5,
    'everyteam': 78.2, 'signature': 82.0,
}
NO_CAPTION = {'justask', 'signature', 'q_wow', 'q_cross', 'q_pics', 'q_teams', 'q_report'}
VO = {c['key']: c for c in json.loads((HERE / 'audio/vo_cues.json').read_text())}

# Scenes built on the 75 s clock are re-timed with piecewise-linear (new, old) maps: typing stretches
# to match the spoken prompt, everything else keeps its pace.
WARPS = {
    's1': [(0.0, 0.0), (6.25, 5.95), (9.14, 7.05), (9.37, 7.12), (12.85, 10.6)],
    's3': [(19.0, 13.0), (25.6, 20.2)],
    's4': [(25.6, 20.2), (25.85, 20.45), (31.85, 22.35), (32.4, 22.5), (36.85, 26.55)],
    's5': [(40.85, 27.9), (45.55, 32.6)],
    's6': [(45.55, 32.6), (45.8, 32.85), (49.65, 34.3), (50.05, 34.4), (53.4, 37.45)],
    's11': [(78.0, 62.8), (81.7, 66.5)],
}


def _interp(pts, v, src, dst):
    if v <= pts[0][src]:
        return pts[0][dst] + (v - pts[0][src])
    for p, q in zip(pts, pts[1:]):
        if v <= q[src]:
            return p[dst] + (v - p[src]) * (q[dst] - p[dst]) / (q[src] - p[src])
    return pts[-1][dst] + (v - pts[-1][src])


def to_old(key, t):
    return _interp(WARPS[key], t, 0, 1)


def to_new(key, t):
    """Map a time on the old 75 s clock to the new film clock (used for sound effects)."""
    return _interp(WARPS[key], t, 1, 0)


def warped(key, fn):
    def f(t):
        return fn(to_old(key, t))
    return f


END_T0 = 81.7


def end_card(t):
    na = MANIFEST['old_frames']['end_a'] / FPS
    if t < END_T0 + na:
        return old_frame('end_a', int(round((t - END_T0) * FPS))).copy()
    cur = old_frame('end_b', int(round((t - END_T0 - na) * FPS))).copy()
    u = smooth(prog(t, END_T0 + na, END_T0 + na + .26))
    if u < 1:
        cur = Image.blend(old_frame('end_a', 10 ** 6), cur, u)
    k = smooth(prog(t, DURATION - .6, DURATION))
    if k > 0:
        overlay(cur, (0, 0, 0), k)
    return cur


SCENES = [
    (0.0, 12.85, warped('s1', A.s1)),
    (12.85, 19.0, C.brand),
    (19.0, 25.6, warped('s3', A.s3)),
    (25.6, 36.85, warped('s4', A.s4)),
    (36.85, 40.85, C.part3d),
    (40.85, 45.55, warped('s5', A.s5)),
    (45.55, 53.4, warped('s6', A.s6)),
    (53.4, 74.2, B.s7),
    (74.2, 78.0, B.s8),
    (78.0, 81.7, warped('s11', B.s11)),
    (81.7, DURATION + .01, end_card),
]
TRANSITIONS = {12.85: ('flash', .5), 19.0: ('fade', .4), 25.6: ('fade', .35), 36.85: ('fade', .35),
               40.85: ('fade', .35), 45.55: ('fade', .35), 53.4: ('fade', .35), 74.2: ('fade', .35),
               78.0: ('fade', .35), 81.7: ('flash', .44)}


def scene_index(t):
    for i, (a, b, _) in enumerate(SCENES):
        if a <= t < b:
            return i
    return len(SCENES) - 1


def picture(t):
    i = scene_index(t)
    start, _, fn = SCENES[i]
    kind, d = TRANSITIONS.get(start, (None, 0))
    # flash out of the previous scene
    if i + 1 < len(SCENES):
        nstart = SCENES[i + 1][0]
        nkind, nd = TRANSITIONS.get(nstart, (None, 0))
        if nkind == 'flash' and t >= nstart - nd / 2:
            img = fn(t)
            overlay(img, WHITE, smooth(prog(t, nstart - nd / 2, nstart)))
            return img
    img = fn(t)
    if kind == 'fade' and i > 0 and t < start + d:
        prev = SCENES[i - 1][2](t).convert('RGBA')
        img = Image.blend(prev, img.convert('RGBA'), smooth((t - start) / d))
    elif kind == 'flash' and t < start + d / 2:
        overlay(img, WHITE, 1 - smooth(prog(t, start, start + d / 2)))
    return img


@lru_cache(maxsize=32)
def caption_sprite(key):
    lines = VO[key]['caption'].split('\n')
    size, lh = 38, 52
    widths = [tw(l, size, 'demi') for l in lines]
    w = int(max(widths)) + 64
    h = lh * len(lines) + 26
    spr = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    put(spr, rrect(w, h, 22, (10, 20, 38), 190), 0, 0)
    for i, l in enumerate(lines):
        text(spr, l, w / 2, 12 + i * lh, size, 'demi', WHITE, anchor='c')
    return spr


def captions(img, t):
    for key, start in VO_START.items():
        if key in NO_CAPTION or key not in VO:
            continue
        end = start + VO[key]['duration']
        if key == 'archive':
            end = min(end, VO_START['q_wow'] - .32)
        if start - .1 <= t <= end + .32:
            a = smooth(prog(t, start - .1, start + .06)) * (1 - smooth(prog(t, end + .12, end + .32)))
            spr = caption_sprite(key)
            put(img, spr, (W - spr.width) / 2, 1036 - spr.height, a)


def frame(t):
    img = picture(t).convert('RGBA')
    captions(img, t)
    return img.convert('RGB')


# ------------------------------------------------------------------ driver
def preview(times):
    PREVIEW.mkdir(exist_ok=True)
    for t in times:
        t0 = time.time()
        frame(t).save(PREVIEW / f'f_{t:06.2f}.jpg', quality=88)
        print(f'{t:6.2f}s  {time.time() - t0:.2f}s')


def sheet(step=1.0, cols=6, w=480):
    PREVIEW.mkdir(exist_ok=True)
    times = [round(i * step + step / 2, 3) for i in range(int(DURATION / step))]
    h = int(w * 9 / 16)
    rows = (len(times) + cols - 1) // cols
    out = Image.new('RGB', (cols * w, rows * (h + 26)), (30, 30, 30))
    with Pool(10) as pool:
        imgs = pool.map(_small, [(t, w, h) for t in times])
    from PIL import ImageDraw
    d = ImageDraw.Draw(out)
    for k, (t, im) in enumerate(zip(times, imgs)):
        x, y = (k % cols) * w, (k // cols) * (h + 26)
        out.paste(im, (x, y + 26))
        d.text((x + 6, y + 4), f'{t:.2f}s', fill=(230, 230, 230), font=F(18, 'demi'))
    out.save(PREVIEW / 'contact_sheet.jpg', quality=85)
    print(PREVIEW / 'contact_sheet.jpg')


def _small(args):
    t, w, h = args
    return frame(t).resize((w, h), Image.LANCZOS)


def _segment(args):
    k, f0, f1 = args
    path = OUT / f'seg_{k:03d}.mp4'
    cmd = [FFMPEG, '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}',
           '-r', str(FPS), '-i', '-', '-c:v', 'libx264', '-preset', 'medium', '-crf', '15',
           '-pix_fmt', 'yuv420p', '-g', '60', str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(f0, f1):
        proc.stdin.write(frame(i / FPS).tobytes())
    proc.stdin.close()
    proc.wait()
    return k, f1 - f0


def render(seg_frames=120, workers=10):
    OUT.mkdir(exist_ok=True)
    total = int(round(DURATION * FPS))
    jobs = [(k, f0, min(total, f0 + seg_frames)) for k, f0 in enumerate(range(0, total, seg_frames))]
    t0 = time.time()
    done = 0
    with Pool(workers) as pool:
        for k, n in pool.imap_unordered(_segment, jobs):
            done += n
            print(f'segment {k:3d} done · {done}/{total} frames · {time.time() - t0:.0f}s', flush=True)
    listing = OUT / 'segments.txt'
    listing.write_text(''.join(f"file 'seg_{k:03d}.mp4'\n" for k, *_ in jobs))
    subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(listing),
                    '-c', 'copy', str(OUT / 'video_silent.mp4')], check=True)
    print('video:', OUT / 'video_silent.mp4', f'{time.time() - t0:.0f}s')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'sheet'
    if cmd == 'preview':
        preview([float(v) for v in sys.argv[2:]])
    elif cmd == 'sheet':
        sheet(float(sys.argv[2]) if len(sys.argv) > 2 else 1.0)
    elif cmd == 'render':
        render()
