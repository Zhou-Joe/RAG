"""Capture the app's 3D Mine Train model (C-frame fly-in) with headless Chromium.

  v2_env/bin/python v2/model3d/capture.py   -> v2/assets/model3d/0000.png ... + meta.json
"""
from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
STATIC = (ROOT if (ROOT / 'manage.py').is_file() else ROOT / 'RAG') / 'static'
OUT = HERE.parent / 'assets/model3d'
PORT = 8765
CHROME = Path.home() / ('Library/Caches/ms-playwright/chromium_headless_shell-1248/'
                        'chrome-headless-shell-mac-arm64/chrome-headless-shell')
TYPES = {'.html': 'text/html', '.mjs': 'text/javascript', '.js': 'text/javascript', '.css': 'text/css',
         '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json'}
done = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith('/static/'):
            file = STATIC / path[len('/static/'):]
        else:
            file = HERE / path.lstrip('/')
        if not file.is_file():
            self.send_error(404)
            return
        data = file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', TYPES.get(file.suffix, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        url = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if url.path == '/frame':
            i = int(parse_qs(url.query)['i'][0])
            (OUT / f'{i:04d}.png').write_bytes(body)
            if i % 40 == 0:
                print('frame', i, flush=True)
        elif url.path == '/meta':
            (OUT / 'meta.json').write_bytes(body)
        elif url.path == '/error':
            print('PAGE ERROR:', body.decode(), flush=True)
            done.set()
        elif url.path == '/done':
            done.set()
        self.send_response(204)
        self.end_headers()


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chrome = subprocess.Popen([
        str(CHROME), '--no-sandbox', '--headless', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
        '--ignore-gpu-blocklist', '--hide-scrollbars', '--force-device-scale-factor=1.5',
        '--window-size=1260,760', '--user-data-dir=/tmp/engenie_chs', f'http://127.0.0.1:{PORT}/harness.html',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = done.wait(timeout=900)
    chrome.kill()
    server.shutdown()
    frames = len(list(OUT.glob('*.png')))
    meta = json.loads((OUT / 'meta.json').read_text()) if (OUT / 'meta.json').exists() else {}
    print('finished' if ok else 'timed out', frames, 'frames', meta.get('width'), meta.get('height'))


if __name__ == '__main__':
    main()
