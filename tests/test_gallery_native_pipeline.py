"""Offline feasibility checks of gallery-dl's actual job/parser/downloader.

The Bilibili page is synthetic and injected; only media requests use loopback.
This does not establish real Bilibili page transport or online acceptance.
"""
import copy
import hashlib
import json
import logging
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from gallery_dl import config, job
from gallery_dl.extractor.bilibili import BilibiliArticleExtractor


def png(color):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + bytes(color) * 2) * 2)) + chunk(b'IEND', b''))


@pytest.fixture
def native_gallery(tmp_path):
    original = copy.deepcopy(config._config)
    config.clear()
    for key, value in {'base-directory': str(tmp_path), 'directory': [], 'filename': '{num:02}.{extension}',
                       'retries': 0, 'timeout': 2, 'sleep-request': 0, 'sleep': 0,
                       'fallback': False, 'postprocessors': [], 'skip': True}.items():
        config.set(('extractor',), key, value)
    config.set(('downloader',), 'retries', 0)
    config.set(('output',), 'mode', 'null')
    state = {'requests': [], 'media': {f'/{i}.png': png((i * 40, 20, 30)) for i in range(1, 5)}, 'fail': None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state['requests'].append({'path': self.path, 'headers': dict(self.headers)})
            payload = state['media'].get(self.path)
            status = 404 if state['fail'] == self.path or payload is None else 200
            self.send_response(status)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(payload) if status == 200 else 0))
            self.end_headers()
            if status == 200:
                self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state['base'] = f'http://127.0.0.1:{server.server_port}'
    try:
        yield state, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        config.clear()
        config._config.update(original)


def run_native(state, blocked=None):
    article = {'id': '1249456774436618276', 'detail': {'modules': [
        {'module_type': 'author', 'module_author': {'mid': 1337856191, 'name': 'synthetic', 'pub_ts': 1}},
        {'module_type': 'content', 'module_blocked': blocked, 'module_content': {'paragraphs': [
            {'pic': {'pics': [{'url': state['base'] + '/' + str(i) + '.png'} for i in range(1, 5)]}}
        ]}}
    ]}}
    extractor = BilibiliArticleExtractor.from_url('https://www.bilibili.com/opus/1249456774436618276')
    # The only page call is injected. No real Bilibili request is permitted here.
    pages = []
    def synthetic_page(url, **kwargs):
        assert url == 'https://www.bilibili.com/opus/1249456774436618276'
        pages.append(url)
        return SimpleNamespace(text='window.__INITIAL_STATE__=' + json.dumps(article) + ';')
    extractor.request = synthetic_page
    extractor.initialize()
    extractor.session.trust_env = False
    original_request = extractor.session.request
    def local_only(method, url, **kwargs):
        assert url.startswith(state['base'] + '/')
        return original_request(method, url, **kwargs)
    extractor.session.request = local_only
    work = job.DownloadJob(extractor)
    try:
        work.run()
        return work.status, pages
    finally:
        extractor.session.close()


def test_native_parser_job_and_http_preserve_four_pngs(native_gallery):
    state, directory = native_gallery
    status, pages = run_native(state)
    assert status == 0 and len(pages) == 1
    assert [r['path'] for r in state['requests']] == ['/1.png', '/2.png', '/3.png', '/4.png']
    files = sorted(directory.glob('*.png'))
    assert [p.name for p in files] == ['01.png', '02.png', '03.png', '04.png']
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in files] == [
        hashlib.sha256(state['media'][f'/{i}.png']).hexdigest() for i in range(1, 5)]
    assert all(r['headers'].get('User-Agent') for r in state['requests'])
    assert not list(directory.glob('*.part'))


def test_native_zero_retries_still_continues_later_assets(native_gallery):
    state, directory = native_gallery
    state['fail'] = '/1.png'
    status, pages = run_native(state)
    assert status != 0 and len(pages) == 1
    assert [r['path'] for r in state['requests']] == ['/1.png', '/2.png', '/3.png', '/4.png']
    assert not (directory / '01.png').exists()
    assert len(list(directory.glob('*.png'))) == 3


def test_native_nonempty_blocked_module_is_warning_not_stop(native_gallery, caplog):
    state, directory = native_gallery
    with caplog.at_level(logging.WARNING):
        status, _ = run_native(state, {'hint_message': 'SYNTHETIC_BLOCKED_SENTINEL'})
    assert status == 0 and len(list(directory.glob('*.png'))) == 4
    assert 'SYNTHETIC_BLOCKED_SENTINEL' in caplog.text
    # This is a detected upstream behavior gap, not permission to run online.
