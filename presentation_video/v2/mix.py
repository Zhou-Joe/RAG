"""Final V2 soundtrack: narration + ducked music bed + procedural sound design.

  v2_env/bin/python v2/mix.py      -> v2/audio/final_mix.wav (48 kHz stereo, loudness-normalised)
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, resample_poly, sosfilt

HERE = Path(__file__).resolve().parent
AUDIO = HERE / 'audio'
SR = 48000
DUR = 85.0
FFMPEG = '/opt/homebrew/bin/ffmpeg'
RNG = np.random.default_rng(5)

import sys  # noqa: E402
sys.path.insert(0, str(HERE))
from film import VO_START, to_new  # noqa: E402
from scenes_a import LABELS  # noqa: E402
from scenes_b import BIG_WORDS, BUILDER_T, CODE_FADE, JUST_ASK_T, MSGS, REPORT_CLICK, TEAMS_T, WITH_GENIE_T  # noqa: E402

VO_GAIN_TARGET = .12   # median short-term RMS of speech
DUCK = .5


def n(sec):
    return int(round(sec * SR))


def stereo(x, pan=0.0):
    return np.column_stack((x * np.sqrt((1 - pan) / 2) * 1.414, x * np.sqrt((1 + pan) / 2) * 1.414))


# ------------------------------------------------------------------ sound design
def click(vol=1.0, f=910):
    t = np.arange(n(.105)) / SR
    env = np.minimum(t / .003, 1.) * np.exp(-t / .016) * np.clip((.105 - t) / .035, 0., 1.)
    return .029 * vol * env * (np.sin(2 * np.pi * f * t) + .18 * np.sin(2 * np.pi * 2.05 * f * t))


def tick(vol=1.0, f=2400):
    t = np.arange(n(.04)) / SR
    env = np.exp(-t / .006)
    noise = RNG.normal(0, 1, len(t))
    return .012 * vol * env * (.6 * np.sin(2 * np.pi * f * t) + .4 * noise)


def chime(notes, vol=.03, decay=.16, length=None):
    length = length or (max(s for s, _ in notes) + decay * 6)
    t = np.arange(n(length)) / SR
    out = np.zeros(len(t))
    for start, f in notes:
        dt = np.maximum(t - start, 0.)
        env = np.minimum(dt / .014, 1.) * np.exp(-dt / decay) * (t >= start)
        out += env * (np.sin(2 * np.pi * f * dt) + .11 * np.sin(4 * np.pi * f * dt))
    return vol * out * np.clip((length - t) / .06, 0, 1)


def band_noise(length, lo, hi):
    sos = butter(2, [lo, hi], btype='band', fs=SR, output='sos')
    return sosfilt(sos, RNG.normal(0, 1, n(length)))


def whoosh(length=.5, vol=.05, lo=300, hi=3000, rise=.45):
    x = band_noise(length, lo, hi)
    t = np.linspace(0, 1, len(x))
    env = np.where(t < rise, (t / rise) ** 2, ((1 - t) / (1 - rise)) ** 1.6)
    return vol * x * env / (np.abs(x).max() + 1e-9) * 3


def swell(length=.7, vol=.06):
    """Reverse-whoosh that pulls in to a hit."""
    x = band_noise(length, 200, 6000)
    t = np.linspace(0, 1, len(x))
    sweep = np.sin(2 * np.pi * np.cumsum(np.linspace(180, 900, len(x))) / SR)
    env = t ** 3
    return vol * env * (x / (np.abs(x).max() + 1e-9) * 2.2 + .35 * sweep)


def impact(vol=.16):
    t = np.arange(n(1.6)) / SR
    boom = np.sin(2 * np.pi * (48 + 40 * np.exp(-t / .08)) * t) * np.exp(-t / .38)
    hit = band_noise(1.6, 800, 9000) * np.exp(-t / .05) * .25
    shimmer = sum(np.sin(2 * np.pi * f * t) * np.exp(-t / .7) for f in (1568, 2093, 2637)) * .05
    return vol * (boom + hit + shimmer) * np.clip((1.6 - t) / .2, 0, 1)


def riser(length=2.0, vol=.05):
    t = np.arange(n(length)) / SR
    u = t / length
    f = 220 * 2 ** (u * 2.2)
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR) * .5 + np.sin(2 * np.pi * np.cumsum(f * 1.5) / SR) * .25
    air = band_noise(length, 1500, 8000) * .25
    return vol * (u ** 2.2) * (tone + air / (np.abs(air).max() + 1e-9))


def sparkle(vol=.025, count=9, length=1.2):
    out = np.zeros(n(length))
    for k in range(count):
        s = k * .05 + RNG.uniform(0, .03)
        f = RNG.choice([1760, 2093, 2349, 2637, 3136, 3520])
        c = chime([(0, f)], vol, .12, length - s)
        out[n(s):n(s) + len(c)] += c[:len(out) - n(s)]
    return out


def flips(t0, t1, pages):
    """Paper rustle on every page turn of the eased flip animation."""
    out = []
    last = 0
    for i in range(1, 400):
        t = t0 + (t1 - t0) * i / 400
        u = (t - t0) / (t1 - t0)
        e = 4 * u ** 3 if u < .5 else 1 - (-2 * u + 2) ** 3 / 2
        idx = int(e * (pages - 1))
        if idx > last:
            out.append(t)
            last = idx
    return out


def typing(t0, t1, rate=22):
    times, t = [], t0
    while t < t1:
        times.append(t)
        t += RNG.uniform(.6, 1.4) / rate
    return times


def sfx_track():
    track = np.zeros((n(DUR) + SR, 2))

    def add(t, x, pan=0.0, gain=1.0):
        s = stereo(np.asarray(x, float) * gain, pan)
        a = n(t)
        b = min(len(track), a + len(s))
        track[a:b] += s[:b - a]

    def w(key):
        return lambda t: to_new(key, t)

    # S1 · opening (old clock until the question is typed)
    o = w('s1')
    add(0.02, impact(.07))
    for k, (t, *_) in enumerate(LABELS):
        add(o(t), chime([(0, [659, 784, 880, 1047, 1175][k])], .022, .2), pan=-.6 + k * .3)
    add(1.0, whoosh(1.4, .045, 200, 2500, .3))
    add(o(3.7), riser(o(5.7) - o(3.7), .05))
    for t in typing(VO_START['q_wow'], o(7.05), 20):
        add(t, tick(.9), pan=RNG.uniform(-.2, .2))
    add(o(7.15), click(1.3, 760))
    add(o(7.27), swell(.7, .07))
    add(o(7.95), impact(.17))
    add(o(8.48), chime([(0, 988), (.09, 1319)], .026))
    add(o(9.42), click(.8))
    add(o(10.38), whoosh(.5, .04, 800, 7000, .7))
    # brand
    add(12.91, chime([(0, 784), (.07, 988), (.14, 1175), (.21, 1568)], .022, .25))
    add(15.25, whoosh(.9, .025, 400, 5000, .5))
    for k, t in enumerate([16.33, 17.02, 17.83]):
        add(t, chime([(0, [880, 1109, 1319][k]), (.07, [1319, 1661, 1976][k])], .02, .16), pan=-.4 + k * .4)
    # S3 · any file
    o = w('s3')
    for i in range(5):
        add(o(13.05 + i * .09), click(.55, 820 + i * 60), pan=-.6 + i * .3)
    add(o(13.85), chime([(0, 880), (.08, 1175)], .018))
    add(o(14.45), whoosh(.5, .035))
    for t in flips(o(14.95), o(16.05), 28):
        add(t, whoosh(.09, .022, 1500, 7000, .2), pan=.25)
    add(o(16.1), chime([(0, 1319)], .022, .22))
    add(o(16.55), whoosh(.55, .03))
    add(o(17.13), click(.7))
    add(o(17.3), whoosh(.45, .035))
    for t in typing(o(17.6), o(18.9), 30):
        add(t, tick(.5, 1800), pan=-.2)
    add(o(19.02), chime([(0, 1319)], .022, .22))
    add(o(19.45), whoosh(.55, .03))
    add(o(20.03), click(.7))
    # S4 · cross-check -> 3D
    o = w('s4')
    for t in typing(VO_START['q_cross'], o(22.35), 14):
        add(t, tick(.85), pan=.3)
    add(o(22.5), click(1.1))
    for i in range(3):
        add(o(22.8 + i * .22), whoosh(.35, .028, 600, 5000, .6), pan=.5)
    for i in range(3):
        add(o(23.7 + i * .1), chime([(0, 1175 + i * 150)], .016, .1))
    add(o(24.0), chime([(0, 880), (.08, 1109), (.16, 1319)], .02))
    for t in (24.2, 24.75, 25.3):
        add(o(t), click(.7, 700))
    add(o(25.55), chime([(0, 1568)], .012, .1))
    add(o(25.7), chime([(0, 1047), (.1, 1568)], .026, .2))
    add(o(26.2), click(1.1))
    add(36.75, whoosh(.6, .035, 400, 6000, .5))
    add(37.0, riser(1.9, .03))
    for k in range(4):
        add(36.85 + [.25, 1.25, 1.95, 2.55][k], click(.5, 900 + k * 90), pan=.5)
    add(39.45, chime([(0, 988), (.09, 1319), (.18, 1760)], .026, .2))
    add(39.72, whoosh(.3, .02, 1500, 7000, .5))
    # S5 · tracker
    o = w('s5')
    add(o(27.95), whoosh(.8, .03, 500, 6000, .25))
    for i in range(0, 62, 3):
        add(o(27.95 + i * .013), tick(.35, 3000), pan=RNG.uniform(-.6, .1))
    for k in range(10):
        add(o(29.05 + k * .2), click(.45, 980 + k * 25), pan=.3)
    add(o(31.3), chime([(0, 740), (.12, 740)], .028, .12))
    # S6 · pictures
    o = w('s6')
    for t in typing(VO_START['q_pics'], o(34.3), 16):
        add(t, tick(.85), pan=.3)
    add(o(34.4), click(1.1))
    add(o(34.45), whoosh(.95, .03, 1000, 9000, .5), pan=0)
    for i in range(4):
        add(o(35.45 + i * .16), chime([(0, [988, 1175, 1319, 1568][i])], .016, .12), pan=-.3 + i * .2)
    # S7 · what it normally takes -> just ask -> Teams alert -> weekly report
    for i in range(8):
        add(53.7 + i * .3, click(.45, 520 + i * 30), pan=RNG.uniform(-.7, .7))
    for k, t in enumerate([55.05, 55.92, 56.45]):
        add(t, click(.9, 640 + k * 60))
    add(WITH_GENIE_T, chime([(0, 784), (.14, 988)], .012, .28))
    for k, (_, t) in enumerate(BIG_WORDS):
        add(t, chime([(0, [880, 988][k])], .009, .24))
    add(CODE_FADE[0], whoosh(.9, .012, 300, 3000, .5))
    add(JUST_ASK_T, chime([(0, 784), (.14, 1047), (.28, 1319)], .015, .28))
    add(BUILDER_T, whoosh(.65, .018, 400, 3500, .5))
    for t in typing(MSGS[0][2], MSGS[0][3], 16):
        add(t, tick(.75), pan=-.2)
    for k in range(3):
        add(MSGS[1][2] + k * .15, chime([(0, 1175 + k * 196)], .014, .09), pan=-.3)
    add(TEAMS_T, chime([(0, 1047), (.1, 1397), (.2, 1047)], .03, .15), pan=.35)
    for t in typing(MSGS[2][2], MSGS[2][3], 16):
        add(t, tick(.75), pan=-.2)
    add(MSGS[3][2], click(.6))
    add(REPORT_CLICK, click(1.1))
    add(REPORT_CLICK + .04, chime([(0, 1047), (.08, 1319), (.16, 1568)], .026, .2))
    # S8 · weekly email to everyone
    add(74.15, whoosh(.5, .03))
    add(74.3, chime([(0, 1319), (.11, 988)], .028, .18))
    add(75.0, sparkle(.012, 6, .9))
    for i in range(4):
        add(75.55 + i * .16, whoosh(.3, .018, 900, 6000, .5), pan=.2 + i * .12)
        add(76.1 + i * .16, chime([(0, 1568)], .011, .08), pan=.3 + i * .1)
    # S11 · every team
    o = w('s11')
    penta = [523, 587, 659, 784, 880, 1047, 1175, 1319, 1568, 1760, 2093]
    for k in range(11):
        add(o(62.85 + k * .06), chime([(0, penta[k])], .011, .1), pan=-.4 + k * .08)
    for i in range(3):
        add(o(64.25 + i * .12), click(.6, 700 + i * 80), pan=-.6)
    add(o(65.3), chime([(0, 1175), (.08, 1568)], .016))
    add(o(65.7), click(.8, 600))
    # end card
    add(80.92, swell(.75, .06))
    add(81.62, sparkle(.02, 10, 1.4))
    add(81.78, chime([(0, 784), (.08, 988), (.16, 1175), (.24, 1568), (.32, 2093)], .022, .3))
    return track[:n(DUR)]


# ------------------------------------------------------------------ voice + music
def voice_track():
    cues = {c['key']: c for c in json.loads((AUDIO / 'vo_cues.json').read_text())}
    track = np.zeros(n(DUR))
    activity = np.zeros(n(DUR))
    clips, levels = {}, {}
    for key in VO_START:
        path = Path(cues[key]['file'])
        if not path.is_absolute():
            path = AUDIO / path
        elif not path.is_file():
            path = AUDIO / 'vo' / path.name
        data, sr = sf.read(path)
        if data.ndim > 1:
            data = data.mean(1)
        if sr != SR:
            data = resample_poly(data, SR, sr)
        clips[key] = data
        hop = SR // 10
        r = np.array([np.sqrt(np.mean(data[i:i + hop] ** 2)) for i in range(0, len(data) - hop, hop)])
        levels.setdefault(cues[key]['voice'], []).append(np.median(r[r > .01]))
    gains = {v: VO_GAIN_TARGET / float(np.median(l)) for v, l in levels.items()}
    for key, start in VO_START.items():
        data = clips[key] * gains[cues[key]['voice']]
        a = n(start)
        b = min(len(track), a + len(data))
        track[a:b] += data[:b - a]
        activity[a:b] = 1.0
    ramp = n(.25)
    kernel = np.ones(ramp) / ramp
    activity = np.clip(np.convolve(activity, kernel, mode='same') * 1.0, 0, 1)
    activity = np.maximum(activity, np.convolve(activity, kernel, mode='same'))
    print('voice gains', {v: round(g, 2) for v, g in gains.items()})
    return track, activity


def limit(mix, thr):
    from scipy.ndimage import maximum_filter1d, minimum_filter1d, uniform_filter1d
    env = maximum_filter1d(np.abs(mix).max(1), size=n(.012))
    g = np.minimum(1.0, thr / np.maximum(env, 1e-9))
    g = minimum_filter1d(g, size=n(.02))
    g = uniform_filter1d(g, size=n(.02))
    return mix * g[:, None]


def loudnorm(src, dst, target=-16.0):
    first = subprocess.run([FFMPEG, '-hide_banner', '-i', str(src), '-af',
                            f'loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'],
                           capture_output=True, text=True)
    stats = json.loads(re.findall(r'\{[^{}]+\}', first.stderr)[-1])
    af = (f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
          f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
          f"offset={stats['target_offset']}:linear=true")
    subprocess.run([FFMPEG, '-y', '-v', 'error', '-i', str(src), '-af', af, '-ar', str(SR), str(dst)], check=True)
    print('loudness in', stats['input_i'], 'LUFS ->', target)


def main():
    music, sr = sf.read(AUDIO / 'music_bed.wav')
    assert sr == SR
    music = music[:n(DUR)]
    voice, activity = voice_track()
    voice = limit(voice[:, None], .7)[:, 0]
    music = music * (1 - DUCK * activity)[:, None]
    mix = music + stereo(voice) + sfx_track()
    peak = np.abs(mix).max()
    mix = limit(mix, .62)
    raw = AUDIO / 'mix_raw.wav'
    sf.write(raw, mix.astype(np.float32), SR)
    loudnorm(raw, AUDIO / 'final_mix.wav')
    print('peak before norm', round(float(peak), 3), '->', AUDIO / 'final_mix.wav')


if __name__ == '__main__':
    main()
