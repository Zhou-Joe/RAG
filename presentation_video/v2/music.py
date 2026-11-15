"""Recover the V22 music bed (mix minus narration minus effects, un-ducked) and re-edit it to 85 s."""
from pathlib import Path
import json
import sys

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
OLD = HERE.parent
SR = 48000
OLD_PLAN = json.loads((OLD / 'audio_plan.json').read_text())
OLD_CUES = json.loads((OLD / 'voice_cues_smooth.json').read_text())


def frame(t):
    return int(round(t * SR))


def chime(duration, frequencies, volume=.030, pan=0.):
    t = np.arange(frame(duration)) / SR
    wave = np.zeros(len(t))
    for start, f in frequencies:
        dt = np.maximum(t - start, 0.)
        env = np.minimum(dt / .014, 1.) * np.exp(-dt / .16) * (t >= start)
        wave += env * (np.sin(2 * np.pi * f * dt) + .11 * np.sin(4 * np.pi * f * dt))
    wave *= volume * np.clip((duration - t) / .060, 0., 1.)
    return np.column_stack((wave * np.sqrt(1 - pan), wave * np.sqrt(1 + pan)))


def click():
    t = np.arange(frame(.105)) / SR
    env = np.minimum(t / .003, 1.) * np.exp(-t / .016) * np.clip((.105 - t) / .035, 0., 1.)
    wave = .029 * env * (np.sin(2 * np.pi * 910 * t) + .18 * np.sin(2 * np.pi * 1870 * t))
    return np.column_stack((wave, wave * .88))


def old_effects(n):
    fx = np.zeros((n, 2))

    def add(at, clip):
        a = frame(at)
        fx[a:a + len(clip)] += clip[:max(0, n - a)]
    for at in OLD_PLAN['clicks']:
        add(at, click())
    for at in OLD_PLAN['notifications']:
        add(at, chime(.72, ((0., 659.25), (.135, 880.)), volume=.052, pan=.14))
    for at in OLD_PLAN['reveals']:
        add(at, chime(.93, ((0., 587.33), (.107, 739.99), (.227, 880.)), volume=.026))
    return fx


def activity(t, ramp):
    """Smoothed 0..1 narration activity used for the old ducking."""
    act = np.zeros_like(t)
    for cue in OLD_CUES:
        a, b = cue['start'], cue.get('audible_end', cue['end'])
        rise = np.clip((t - (a - ramp)) / ramp, 0, 1)
        fall = np.clip(((b + ramp) - t) / ramp, 0, 1)
        act = np.maximum(act, np.minimum(rise, fall))
    return act * act * (3 - 2 * act)


def recovered(ramp=.25):
    mix, sr = sf.read(OLD / 'final_mix_smooth.wav')
    voice, _ = sf.read(OLD / 'narration_smooth.wav')
    assert sr == SR
    bed = mix - voice - old_effects(len(mix))
    t = np.arange(len(bed)) / SR
    bed /= (1 - .53 * activity(t, ramp))[:, None]
    return bed


def roughness(bed):
    """Mean jump of log-RMS (20 ms windows) around old cue edges; lower means smoother un-ducking."""
    w = 960
    mono = bed.mean(1)
    k = len(mono) // w
    env = np.log(np.sqrt((mono[:k * w].reshape(k, w) ** 2).mean(1)) + 1e-5)
    jumps = []
    for cue in OLD_CUES:
        for edge in (cue['start'], cue.get('audible_end', cue['end'])):
            i = int(edge * SR / w)
            if 10 < i < k - 10:
                jumps.append(abs(env[i - 8:i - 2].mean() - env[i + 2:i + 8].mean()))
    return float(np.mean(jumps))


def source_buffer(bed):
    """Map the old timeline back onto music source time (V22 audio_plan music edits)."""
    src = np.zeros((frame(127), 2))
    have = np.zeros(frame(127), bool)
    for start, end, offset in OLD_PLAN['music']:
        a, b = start + .4, end - .4
        if start == 0:
            a = 2.9  # first 2.8 s were copied from an older mix
        s0 = frame(offset + (a - start))
        seg = bed[frame(a):frame(b)]
        free = ~have[s0:s0 + len(seg)]
        src[s0:s0 + len(seg)][free] = seg[free]
        have[s0:s0 + len(seg)] |= free
    return src, have


# new film: (film start, film end, source offset)
EDIT = [(0.0, 5.9, 2.9), (10.25, 53.4, 21.4), (53.4, 70.2, 70.75), (70.2, 78.1, 94.75), (78.1, 85.0, 118.9)]
RESUME = 10.25  # music returns with the answer after the silent question


def build(duration=85.0, xfade=.35):
    bed = recovered()
    src, have = source_buffer(bed)
    out = np.zeros((frame(duration), 2))
    for i, (a, b, off) in enumerate(EDIT):
        pre = xfade if i else 0
        post = xfade if i < len(EDIT) - 1 and EDIT[i + 1][0] == b else 0
        s0, s1 = frame(off - pre), frame(off + (b - a) + post)
        clip = src[s0:s1].copy()
        missing = ~have[s0:s1]
        if missing.mean() > .02:
            print(f'warning: edit {i} has {missing.mean():.1%} missing source', file=sys.stderr)
        n_pre, n_post = frame(2 * pre), frame(2 * post)
        if n_pre:
            clip[:n_pre] *= np.sin(np.linspace(0, np.pi / 2, n_pre))[:, None] ** 2
        if n_post:
            clip[-n_post:] *= np.cos(np.linspace(0, np.pi / 2, n_post))[:, None] ** 2
        o = frame(a - pre)
        out[o:o + len(clip)] += clip[:len(out) - o]
    # opening section falls away into the freeze at 5.7 s
    t = np.arange(len(out)) / SR
    out *= np.where(t < RESUME, np.clip((6.0 - t) / .5, 0, 1), 1)[:, None]
    # final fade
    out *= (np.clip((duration - t) / 1.6, 0, 1) ** 1.3)[:, None]
    return out


if __name__ == '__main__':
    if 'fit' in sys.argv:
        for ramp in (.08, .15, .25, .35, .5):
            print(ramp, round(roughness(recovered(ramp)), 4))
    else:
        music = build()
        (HERE / 'audio').mkdir(exist_ok=True)
        sf.write(HERE / 'audio/music_bed.wav', music.astype(np.float32), SR)
        print('music rms', float(np.sqrt(np.mean(music ** 2))), 'peak', float(np.abs(music).max()))
