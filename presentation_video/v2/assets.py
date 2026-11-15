"""Build every still asset the V2 film needs from the real EnGenie data.

Run once:  v2_env/bin/python v2/assets.py
Outputs land in v2/assets/ (PNG/JPG + assets.json with highlight boxes).
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import warnings
from pathlib import Path

warnings.filterwarnings('ignore')
import fitz  # noqa: E402
import openpyxl  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RAG = ROOT if (ROOT / 'manage.py').is_file() else ROOT / 'RAG'
DOCS = RAG / 'media/documents'
DOC_IMAGES = RAG / 'data/doc_images'
OLD = HERE.parent
OUT = HERE / 'assets'
FFMPEG = '/opt/homebrew/bin/ffmpeg'

AVENIR = '/System/Library/Fonts/Avenir Next.ttc'
MANUAL_MT = '七个小矮人矿山车_游乐设施机械与控制系统EN_Rev_M.pdf'
MANUAL_TRON = '创极速光轮_游乐设施机械与控制系统EN_Rev_L.pdf'
OIL_HERO = '32868274_Slinky_Kicker_Wheel1.pdf'
CITED = {
    'water_graph': 'f79ffbfe52accda2c2e796176be69d956752fcc1a29e90c924adbd9e578f6d6c.jpg',
    'mt_topview': '672d20ae96689250475276c6436cf9b64b4c78a412c6ed0a65861b389b53bef5.jpg',
    'tron_track': '660539e04c6040bb2cabef6c414f5356a595a9062ba78c00035f726931be9451.jpg',
}
CHAT_ASSETS = OLD / 'v2_source_review/chat_assets'

random.seed(7)


def font(size, index=5):
    return ImageFont.truetype(AVENIR, size, index=index)


def page_image(pdf, index, *, width=None, height=None):
    doc = fitz.open(str(pdf))
    page = doc[index]
    zoom = (width / page.rect.width) if width else (height / page.rect.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
    doc.close()
    return img, zoom


def save(img, name, quality=92):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == '.jpg':
        img.convert('RGB').save(path, quality=quality)
    else:
        img.save(path)
    return path


# ------------------------------------------------------------------ sheets
EXCEL_GREEN = (33, 115, 70)
GRID = (218, 222, 226)


def sheet_rows(xlsx, start, stop, cols):
    wb = openpyxl.load_workbook(xlsx, read_only=True)
    ws = wb.worksheets[0]
    header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    rows = []
    for number, row in enumerate(ws.iter_rows(min_row=start, max_row=stop, values_only=True), start=start):
        rows.append((number, [('' if row[c] is None else str(row[c])) for c in cols]))
    wb.close()
    return [str(header[c]) for c in cols], rows


def draw_sheet(title, header, rows, widths, *, row_h=30, highlight=None, with_title=True):
    """Excel-looking grid. Returns image plus y of each row (for highlight math)."""
    num_w = 64
    width = num_w + sum(widths)
    top = (46 if with_title else 0) + 30 + row_h
    height = top + row_h * len(rows)
    img = Image.new('RGB', (width, height), 'white')
    d = ImageDraw.Draw(img)
    f_cell, f_head, f_num = font(17, 5), font(17, 2), font(15, 5)
    y = 0
    if with_title:
        d.rectangle((0, 0, width, 46), fill=EXCEL_GREEN)
        d.text((18, 11), title, font=font(19, 2), fill='white')
        y = 46
    d.rectangle((0, y, width, y + 30), fill=(243, 244, 246))
    x = num_w
    for i, w in enumerate(widths):
        d.text((x + w / 2, y + 15), chr(65 + i), font=f_num, fill=(96, 104, 112), anchor='mm')
        d.line((x, y, x, height), fill=GRID)
        x += w
    y += 30
    d.rectangle((0, y, width, y + row_h), fill=(232, 241, 236))
    x = num_w
    for text, w in zip(header, widths):
        d.text((x + 8, y + row_h / 2), text[:24], font=f_head, fill=(30, 40, 50), anchor='lm')
        x += w
    d.text((num_w / 2, y + row_h / 2), '1', font=f_num, fill=(96, 104, 112), anchor='mm')
    y += row_h
    row_y = {}
    for number, cells in rows:
        row_y[number] = y
        if highlight == number:
            d.rectangle((num_w, y, width, y + row_h), fill=(255, 241, 196))
        d.rectangle((0, y, num_w, y + row_h), fill=(243, 244, 246))
        d.text((num_w / 2, y + row_h / 2), str(number), font=f_num, fill=(96, 104, 112), anchor='mm')
        x = num_w
        for text, w in zip(cells, widths):
            limit = max(4, int(w / 9.2))
            d.text((x + 8, y + row_h / 2), text[:limit], font=f_cell, fill=(34, 40, 48), anchor='lm')
            x += w
        y += row_h
        d.line((0, y, width, y), fill=GRID)
    d.line((num_w, 0 if not with_title else 46, num_w, height), fill=GRID)
    return img, row_y


def build_sheets(manifest):
    atlas = DOCS / 'Atlas_Components_-_Mine_Train.xlsx'
    cols = [0, 3, 4, 6, 7, 8]
    widths = [150, 150, 360, 190, 110, 130]
    header, rows = sheet_rows(atlas, 2440, 2660, cols)
    strip, row_y = draw_sheet('', header, rows, widths, row_h=34, highlight=None, with_title=False)
    head_h = 30 + 34
    save(strip.crop((0, head_h, strip.width, strip.height)), 'sheet/atlas_strip.png')
    save(strip.crop((0, 0, strip.width, head_h)), 'sheet/atlas_header.png')
    manifest['atlas_strip'] = {
        'first_row': 2440, 'row_h': 34, 'target_row': 2620,
        'target_y': row_y[2620] - head_h, 'width': strip.width,
        'columns': widths, 'num_w': 64,
    }
    # storm-sized spreadsheet cards from several real workbooks
    picks = [
        ('Atlas_Components_-_Mine_Train.xlsx', 2610, cols),
        ('Atlas_Components_-_Tron.xlsx', 2, cols),
        ('Atlas_Components_-_Pirates.xlsx', 2, cols),
        ('CYCLE_SHOP各景点备件库位库存2026-11-11.xlsx', 2, None),
    ]
    made = []
    for name, start, use_cols in picks:
        path = DOCS / name
        if not path.exists():
            alts = sorted(DOCS.glob(name.split('_-_')[0] + '*.xlsx')) if '_-_' in name else []
            alts = [a for a in alts if a.name not in [m for m, *_ in picks[:len(made)]]]
            if not alts:
                continue
            path = alts[len(made) % len(alts)]
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.worksheets[0]
        hdr = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
        if use_cols is None:
            use_cols = [i for i, h in enumerate(hdr) if h not in (None, '')][:5]
        wb.close()
        h, r = sheet_rows(path, max(2, start), max(2, start) + 13, use_cols)
        w = [140, 140, 260, 170, 90, 110][:len(use_cols)]
        img, _ = draw_sheet(path.name, h, r, w, row_h=28)
        made.append(save(img, f'storm/sheet_{len(made)}.png').name)
    manifest['storm_sheets'] = made


# ------------------------------------------------------------------ pdfs
def build_pdfs(manifest):
    mt = DOCS / MANUAL_MT
    cover, _ = page_image(mt, 0, height=1240)
    save(cover, 'pdf/mt_cover.jpg')

    flips = [0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 71, 89, 120, 150, 180, 210, 240, 268,
             296, 320, 340, 356, 368, 377, 383, 386, 388, 389]
    manifest['flip_pages'] = flips
    for i, idx in enumerate(flips):
        img, _ = page_image(mt, idx, height=880)
        save(img, f'flip/{i:02d}.jpg', quality=88)

    p390, zoom = page_image(mt, 389, height=1500)
    save(p390, 'pdf/mt_p390.png')
    manifest['p390_row'] = [round(v * zoom) for v in (72, 151, 524, 197)]
    manifest['p390_size'] = list(p390.size)

    oil, zoom = page_image(DOCS / OIL_HERO, 0, height=1500)
    save(oil, 'pdf/oil_p1.png')
    pt = 595.44 / 720.0
    manifest['oil_diag'] = [round(v * pt * zoom) for v in (16, 283, 669, 334)]
    manifest['oil_graph'] = [round(v * pt * zoom) for v in (30, 524, 346, 642)]
    manifest['oil_size'] = list(oil.size)

    oils = sorted(DOCS.glob('3286*.pdf'))
    manifest['oil_count'] = len(oils)
    for i, pdf in enumerate(oils):
        img, _ = page_image(pdf, 0, width=210)
        save(img, f'oil_thumbs/{i:02d}.jpg', quality=85)

    storm = []

    def add(pdf, idx, tag, width=330):
        try:
            img, _ = page_image(pdf, idx, width=width)
        except Exception as exc:  # noqa: BLE001
            print('skip', pdf.name, idx, exc)
            return
        storm.append({'file': save(img, f'storm/{len(storm):02d}_{tag}.jpg', 86).name, 'tag': tag})

    for pdf in sorted(DOCS.glob('*游乐设施*EN_Rev_*.pdf')):
        add(pdf, 0, 'manual')
    add(mt, 71, 'manual_fig', 360)
    add(DOCS / MANUAL_TRON, 133, 'manual_fig', 360)
    add(mt, 389, 'manual_page')
    for pdf, idx in [(DOCS / MANUAL_TRON, 60), (DOCS / MANUAL_TRON, 410), (mt, 150), (mt, 600)]:
        add(pdf, idx, 'manual_page')
    for name in ['191041.pdf', '193171_APP_A.pdf', 'TS-W01_Rev._7_Welding_requirements.pdf',
                 'RTE_1000_Rev_X_Material_requirements.pdf']:
        if (DOCS / name).exists():
            add(DOCS / name, 0, 'standard')
    ts = sorted(DOCS.glob('TS-*.pdf'))
    for pdf in random.sample(ts, min(9, len(ts))):
        add(pdf, 0, 'standard')
    for name in ['Weekly_Report_-_20261106.pdf', 'SDLSPDRTP0003_EN.pdf']:
        if (DOCS / name).exists():
            add(DOCS / name, 0, 'report')
    for pdf in random.sample(oils, 12):
        add(pdf, 0, 'oil')
    add(DOCS / OIL_HERO, 1, 'oil')
    manifest['storm_pdfs'] = storm


# ------------------------------------------------------------------ images
def build_images(manifest):
    for key, name in CITED.items():
        src = CHAT_ASSETS / name
        shutil.copy(src, OUT / f'img/{key}.jpg')
    for name in ['1.jpg', '2.jpg']:
        img = Image.open(DOCS / name).convert('RGB')
        img.thumbnail((1400, 1400))
        save(img, f'img/photo_{name}')
    shutil.copy(OUT / 'img/photo_1.jpg', OUT / 'storm/photo_tron.jpg')
    shutil.copy(OUT / 'img/photo_2.jpg', OUT / 'storm/photo_minetrain.jpg')

    candidates = [p for p in DOC_IMAGES.glob('*/*.jpg') if p.stat().st_size > 40_000]
    candidates += [p for p in DOC_IMAGES.glob('*/*.png') if p.stat().st_size > 40_000]
    random.shuffle(candidates)
    picked = []
    for path in candidates:
        try:
            img = Image.open(path).convert('RGB')
        except Exception:  # noqa: BLE001
            continue
        if min(img.size) < 220 or max(img.size) / min(img.size) > 2.4:
            continue
        img.thumbnail((360, 360))
        picked.append(save(img, f'figs/{len(picked):02d}.jpg', 86).name)
        if len(picked) >= 30:
            break
    manifest['figs'] = picked

    brand = RAG / 'static'
    for name in ['img/engenie-genie.png', 'img/engenie-front.png', 'img/engenie-look-up.png',
                 'icons/engenie-email-wordmark.png']:
        shutil.copy(brand / name, OUT / 'brand' / Path(name).name)


# ------------------------------------------------------------------ old footage
OLD_CLIPS = {
    'brand': ('Picture_first.mp4', 13.75, 2.0),
    'week': ('Picture_middle.mp4', 20.35, 1.6),
    'end_a': ('Picture_end.mp4', 32.30, 0.94),
    'end_b': ('Picture_end.mp4', 34.80, 1.2),
}


def build_old(manifest):
    counts = {}
    for key, (name, start, dur) in OLD_CLIPS.items():
        folder = OUT / 'old' / key
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        subprocess.run([FFMPEG, '-v', 'error', '-ss', f'{start:.3f}', '-i', str(OLD / name),
                        '-t', f'{dur:.3f}', '-q:v', '2', str(folder / '%04d.jpg')], check=True)
        counts[key] = len(list(folder.glob('*.jpg')))
    manifest['old_frames'] = counts


def main():
    for sub in ['pdf', 'flip', 'oil_thumbs', 'storm', 'sheet', 'img', 'figs', 'brand', 'old']:
        (OUT / sub).mkdir(parents=True, exist_ok=True)
    manifest = {}
    build_pdfs(manifest)
    build_sheets(manifest)
    build_images(manifest)
    build_old(manifest)
    (OUT / 'assets.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    print(json.dumps({k: (v if not isinstance(v, list) or len(v) < 8 else f'{len(v)} items')
                      for k, v in manifest.items()}, indent=1, ensure_ascii=False))


if __name__ == '__main__':
    main()
