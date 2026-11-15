"""Synthesize V2 voice lines with Kokoro and record their durations.

Narrator lines use af_heart; on-screen prompts are read by a second voice (am_michael) so the audience
hears each question being asked. Start times live in film.VO_START.

  v2_env/bin/python v2/narration.py [key ...]   # re-synthesize only the listed keys (all if none exist)
"""
from pathlib import Path
import json
import sys

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

HERE = Path(__file__).resolve().parent
MODELS = HERE.parent / 'v2_deps/tts_models'
OUT = HERE / 'audio/vo'
NARRATOR, ASKER = ('af_heart', 1.0), ('am_michael', 1.1)

# key, voice, caption text, spoken text
LINES = [
    ('archive', NARRATOR, 'Manuals. Standards. Drawings. Lab reports.\nYears of knowledge, scattered everywhere.',
     'Manuals. Standards. Drawings. Lab reports. Years of knowledge, scattered everywhere.'),
    ('q_wow', ASKER, 'Which attraction needs immediate attention? Show me the proof.',
     'Which attraction needs immediate attention? Show me the proof.'),
    ('proof', NARRATOR, 'One question. One answer, with the proof.', 'One question. One answer, with the proof.'),
    ('brand', NARRATOR, 'Introducing En-Genie, your one-stop AI assistant\nto find, verify and deliver knowledge.',
     'Introducing En Genie, your one-stop AI assistant to find, verify, and deliver knowledge.'),
    ('anyfile', NARRATOR, 'Give it any file.', 'Give it any file.'),
    ('manual', NARRATOR, 'A manual with hundreds of pages.', 'A manual with hundreds of pages.'),
    ('sheet', NARRATOR, 'A spreadsheet with thousands of rows.', 'A spreadsheet with thousands of rows.'),
    ('q_cross', ASKER, "What's the C-frame drawing number at Mine Train,\nits NDT requirement, and any MRB records?",
     "What's the C-frame drawing number at Mine Train, its N D T requirement, and any M R B records?"),
    ('cross', NARRATOR, 'One question. Three sources, cross-checked.',
     'One question. Three sources, cross-checked.'),
    ('part3d', NARRATOR, 'And it pinpoints the exact part on the 3D model.',
     'And it pinpoints the exact part, on the three-D model.'),
    ('tracker', NARRATOR, 'A pile of scanned lab reports?\nOne clean tracker, built automatically.',
     'A pile of scanned lab reports? One clean tracker, built automatically.'),
    ('q_pics', ASKER, 'Find the Mine Train and Tron layouts,\nand photos of the real trains.',
     'Find the Mine Train and Tron layouts, and photos of the real trains.'),
    ('pictures', NARRATOR, 'It even searches pictures.',
     'It even searches pictures.'),
    ('bridge', NARRATOR, 'Normally, live data needs engineers, code and setup.',
     'Normally, live data needs engineers, code, and setup.'),
    ('justask', NARRATOR, 'No code. No setup. Just ask.',
     'No code. No setup. Just ask.'),
    ('q_teams', ASKER, 'Every day, tell my team in Teams which trains\nare out of service or on standby.',
     'Every day, tell my team in Teams which trains are out of service, or on standby.'),
    ('teams', NARRATOR, 'Done. The alert lands in Teams, every day.',
     'Done. The alert lands in Teams, every day.'),
    ('q_report', ASKER, 'Every week, email the ride downtime report\nto Mickey, Minnie, Donald and Goofy.',
     'Every week, email the ride downtime report to Mickey, Minnie, Donald, and Goofy.'),
    ('weekly', NARRATOR, 'Every week, on time, with the summary built in.',
     'Every week, on time, with the summary built in.'),
    ('everyteam', NARRATOR, 'Built for every team, with one shared source of knowledge.',
     'Built for every team, with one shared source of knowledge.'),
    ('signature', NARRATOR, 'En-Genie. Engineering the magic.', 'En Genie. Engineering the magic.'),
]


def trim(samples, sr, floor=0.004, pad=0.04):
    """Remove leading and trailing silence, keeping a short pad."""
    loud = np.flatnonzero(np.abs(samples) > floor)
    if not loud.size:
        return samples
    a = max(0, loud[0] - int(pad * sr))
    b = min(len(samples), loud[-1] + int(pad * sr))
    return samples[a:b]


def main(keys):
    OUT.mkdir(parents=True, exist_ok=True)
    tts = Kokoro(str(MODELS / 'kokoro-v1.0.onnx'), str(MODELS / 'voices-v1.0.bin'))
    rows = []
    for key, (voice, speed), caption, spoken in LINES:
        path = OUT / f'{key}.wav'
        if path.exists() and key not in keys:
            data, sr = sf.read(path)
        else:
            data, sr = tts.create(spoken, voice=voice, speed=speed, lang='en-us')
            data = trim(np.asarray(data, dtype=np.float32), sr)
            sf.write(path, data, sr)
        dur = len(data) / sr
        rows.append({'key': key, 'voice': voice, 'duration': round(dur, 3), 'caption': caption,
                     'file': path.relative_to(HERE / 'audio').as_posix()})
        print(f'{key:10s} {voice:10s} {dur:5.2f}s')
    (HERE / 'audio/vo_cues.json').write_text(json.dumps(rows, indent=1, ensure_ascii=False))


if __name__ == '__main__':
    main(set(sys.argv[1:]))
