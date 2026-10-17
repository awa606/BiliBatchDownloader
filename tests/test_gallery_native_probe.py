"""Actual upstream parser/job/downloader with synthetic adapter responses only."""
import copy
import gzip
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from urllib3 import HTTPResponse
from urllib3._collections import HTTPHeaderDict

sys.path.insert(0, str(Path(__file__).parents[1] / 'experiments'))
import gallery_native_probe as probe

SECRET = 'SYNTHETIC_PRIVATE_SENTINEL'


def png(color):
    def chunk(kind, body):
        return struct.pack('!I', len(body)) + kind + body + struct.pack('!I', zlib.crc32(kind + body))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + bytes(color) * 2) * 2)) + chunk(b'IEND', b''))


def response(url, body, status=200, headers=()):
    hdr = HTTPHeaderDict(headers)
    if 'Content-Type' not in hdr:
        hdr.add('Content-Type', 'text/html; charset=utf-8' if body[:1] != b'\x89' else 'image/png')
    result = requests.Response()
    result.status_code, result.url = status, url
    result.headers.update(hdr)
    result.encoding = 'utf-8'
    result.raw = HTTPResponse(body=io.BytesIO(body), status=status, headers=hdr,
                              preload_content=False, decode_content=False)
    return result


class FakeTime:
    def __init__(self):
        self.now = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def native(tmp_path, monkeypatch):
    urls = [f'https://i0.hdslb.com/bfs/{i}.png?token={SECRET}' for i in range(1, 5)]
    article = {'id': probe.SAMPLE, 'detail': {'modules': [
        {'module_type': 'author', 'module_author': {'mid': int(probe.legacy.AUTHOR_UID), 'name': SECRET, 'pub_ts': 1}},
        {'module_type': 'content', 'module_blocked': None, 'module_content': {'paragraphs': [
            {'pic': {'pics': [{'url': url} for url in urls]}}]}}
    ]}}
    state = {'urls': urls, 'article': article, 'calls': [], 'responses': {}, 'time': FakeTime()}
    state['baseline'] = {'author_uid': probe.legacy.AUTHOR_UID, 'assets': [
        probe.media_descriptor(url, {'num': i, 'suffix': '', 'extension': 'png'}) for i, url in enumerate(urls, 1)]}
    state['jar'] = requests.cookies.RequestsCookieJar()
    state['jar'].set('SESSDATA', SECRET, domain='.bilibili.com', path='/')
    state['jar'].set('DedeUserID', '1', domain='.bilibili.com', path='/')
    state['media'] = {url: png((i * 40, 20, 30)) for i, url in enumerate(urls, 1)}

    def send(adapter, request, *args, **kwargs):
        state['calls'].append({'url': request.url, 'method': request.method, 'headers': dict(request.headers),
                               'body': request.body, 'args': args, 'kwargs': kwargs, 'at': state['time'].now})
        if request.url in state['responses']:
            value = state['responses'][request.url]
            if isinstance(value, BaseException):
                raise value
            return response(request.url, *value)
        if request.url == probe.NAV:
            body = json.dumps({'code': 0, 'data': {'isLogin': True, 'mid': 1}}).encode()
        elif request.url == probe.PAGE:
            body = ('window.__INITIAL_STATE__=' + json.dumps(state['article']) + ';').encode()
        elif request.url in state['media']:
            body = state['media'][request.url]
        else:
            pytest.fail('Unexpected endpoint in synthetic transport')
        return response(request.url, body)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    state['guard'] = probe.Guard(clock=state['time'].clock, sleep=state['time'].sleep)
    state['directory'] = tmp_path / 'guarded'
    state['directory'].mkdir()
    return state


def run(state, verifier=None):
    return probe.execute(state['directory'], state['baseline'], state['jar'],
                         verifier or (lambda path, deadline: {'decode': 'PASS'}), guard=state['guard'])


def test_normal_actual_upstream_and_request_semantics_unchanged(native, tmp_path):
    from gallery_dl import job
    from gallery_dl.extractor.bilibili import BilibiliArticleExtractor
    report = run(native)
    assert report['status'] == 'PASS', report
    assert report['network_requests'] == 6 and report['media_requests'] == 4
    observed = copy.deepcopy(native['calls'])
    assert [c['at'] for c in observed] == [0, 1, 2, 3, 4, 5]
    assert all(SECRET not in c['headers'].get('Cookie', '') for c in observed[2:])
    assert [p.name for p in sorted(native['directory'].glob('*.png'))] == ['01.png', '02.png', '03.png', '04.png']
    assert [a['sha256'] for a in report['assets']] == [hashlib.sha256(x).hexdigest() for x in native['media'].values()]
    assert not list(native['directory'].glob('*.part'))
    native['calls'].clear()
    with probe.native_configuration(tmp_path / 'plain'):
        extractor = BilibiliArticleExtractor.from_url(probe.PAGE)
        extractor.initialize()
        extractor.session.cookies.update(native['jar'])
        assert extractor.request(probe.NAV).json()['code'] == 0
        task = job.DownloadJob(extractor)
        task.run()
        assert task.status == 0
        extractor.session.close()
    for guarded, plain in zip(observed, native['calls'], strict=True):
        assert {k: v for k, v in guarded.items() if k != 'at'} == {k: v for k, v in plain.items() if k != 'at'}


@pytest.mark.parametrize('position', [0, 1, 2, 3])
@pytest.mark.parametrize('status', [301, 302, 404, 412, 429, 500])
def test_first_http_failure_stops_whole_job_without_retry(native, position, status):
    url = [probe.NAV, probe.PAGE, *native['urls']][position]
    native['responses'][url] = (SECRET.encode(), status, [('Location', 'https://unknown.invalid/' + SECRET)])
    report = run(native)
    assert report['status'] == 'BLOCKED'
    assert len(native['calls']) == position + 1
    assert report['stop_reason'] == ('redirect_rejected' if status < 400 else 'http_status_stop')
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize('change, reason', [
    ('author', 'author_identity_mismatch'), ('id', 'work_identity_mismatch'),
    ('blocked', 'effective_blocked_module'), ('overwritten_blocked', 'effective_blocked_module'),
    ('missing', 'image_count_mismatch'), ('order', 'fixed_sample_baseline_mismatch'),
    ('path', 'fixed_sample_baseline_mismatch'), ('format', 'media_endpoint_or_format_rejected'),
    ('invalid_blocked', 'blocked_module_type_invalid'),
])
def test_entire_manifest_validated_before_first_media(native, change, reason, caplog, capsys):
    article = native['article']
    modules = article['detail']['modules']
    pics = modules[1]['module_content']['paragraphs'][0]['pic']['pics']
    if change == 'author': modules[0]['module_author']['mid'] = 42
    if change == 'id': article['id'] = '42'
    if change == 'blocked': modules[1]['module_blocked'] = {'hint_message': SECRET}
    if change == 'overwritten_blocked':
        modules[0]['module_blocked'] = {'hint_message': SECRET}
    if change == 'missing': pics.pop()
    if change == 'order': pics.reverse()
    if change == 'path': pics[0]['url'] = 'https://i0.hdslb.com/bfs/changed.png'
    if change == 'format': pics[0]['url'] = pics[0]['url'].replace('.png', '.jpg')
    if change == 'invalid_blocked': modules[1]['module_blocked'] = False
    with caplog.at_level(logging.DEBUG): report = run(native)
    assert report['stop_reason'] == reason, report
    assert len(native['calls']) == 2 and report['media_requests'] == 0
    captured = capsys.readouterr()
    assert SECRET not in json.dumps(report) + caplog.text + captured.out + captured.err


@pytest.mark.parametrize('prefix', ['"module_blocked"', 'var other="module_blocked";'])
def test_marker_outside_initial_data_is_not_effective_block(native, prefix):
    body = (prefix + 'window.__INITIAL_STATE__=' + json.dumps(native['article']) + ';').encode()
    native['responses'][probe.PAGE] = (body,)
    assert run(native)['status'] == 'PASS'


@pytest.mark.parametrize('body', [b'window._riskdata_ = {}', b'window.__riskdata__ = {}'])
@pytest.mark.parametrize('compress', [False, True])
def test_risk_stops_before_upstream_wait_or_media(native, body, compress):
    native['responses'][probe.PAGE] = (gzip.compress(body) if compress else body, 200,
                                      [('Content-Encoding', 'gzip')] if compress else [])
    assert run(native)['stop_reason'] == 'verification_page_detected'
    assert len(native['calls']) == 2


@pytest.mark.parametrize('variant', ['duplicate', 'escaped_duplicate', 'two_assignments', 'invalid', 'utf8'])
def test_ambiguous_initialization_stops(native, variant):
    body = 'window.__INITIAL_STATE__=' + json.dumps(native['article']) + ';'
    if variant == 'duplicate': body = body.replace('"module_blocked": null', '"module_blocked":{"hint_message":"' + SECRET + '"},"module_blocked":null')
    if variant == 'escaped_duplicate': body = body.replace('"module_blocked": null', '"module_\\u0062locked":{},"module_blocked":null')
    if variant == 'two_assignments': body += body
    if variant == 'invalid': body = 'window.__INITIAL_STATE__={bad};'
    payload = b'\xff' if variant == 'utf8' else body.encode()
    native['responses'][probe.PAGE] = (payload,)
    report = run(native)
    assert report['status'] == 'BLOCKED' and len(native['calls']) == 2
    assert report['stop_reason'] in ('page_utf8_invalid', 'page_state_ambiguous_or_invalid')


@pytest.mark.parametrize('data', [{'code': -101}, {'code': 0, 'data': {'isLogin': False, 'mid': 1}},
                                  {'code': 0, 'data': {'isLogin': True, 'mid': 2}}])
def test_account_stops_before_page(native, data):
    native['responses'][probe.NAV] = (json.dumps(data).encode(),)
    assert run(native)['status'] == 'BLOCKED'
    assert len(native['calls']) == 1


@pytest.mark.parametrize('failure', [requests.Timeout(SECRET), requests.ConnectionError(SECRET)])
def test_transport_exception_redacted_and_not_retried(native, failure, capsys, caplog):
    native['responses'][native['urls'][0]] = failure
    report = run(native)
    assert report['stop_reason'] == 'transport_failed' and len(native['calls']) == 3
    captured = capsys.readouterr()
    assert SECRET not in json.dumps(report) + captured.out + captured.err + caplog.text


def test_local_decode_failure_stops_before_second_image(native):
    report = run(native, lambda path, deadline: {'decode': 'FAIL'})
    assert report['stop_reason'] == 'local_decode_failed' and len(native['calls']) == 3
    assert report['assets'][0]['download'] == 'COMPLETE_NOT_DECODED'
    assert (native['directory'] / '01.png').exists()


@pytest.mark.parametrize('case', ['missing', 'raised'])
def test_local_verifier_errors_do_not_continue(native, case):
    def verifier(path, deadline):
        if case == 'raised': raise probe.legacy.ProbeStop(SECRET)
        path.unlink()  # Synthetic test artifact only.
        return {'decode': 'FAIL'}
    report = run(native, verifier)
    assert report['status'] == 'BLOCKED' and len(native['calls']) == 3
    assert SECRET not in json.dumps(report)


def test_native_http_scheme_preserved(native):
    pics = native['article']['detail']['modules'][1]['module_content']['paragraphs'][0]['pic']['pics']
    for item, url in zip(pics, native['urls']):
        item['url'] = url.replace('https:', 'http:')
        native['media'][item['url']] = native['media'][url]
    assert run(native)['status'] == 'PASS'
    assert all(c['url'].startswith('http:') for c in native['calls'][2:])


def test_sensitive_media_header_stops_before_transport(native):
    native['jar'].set('private', SECRET, domain='.hdslb.com', path='/')
    report = run(native)
    assert report['stop_reason'] == 'media_sensitive_header_rejected'
    assert len(native['calls']) == 2


@pytest.mark.parametrize('url', ['https://evil.invalid/bfs/1.png', 'https://i0.hdslb.com:8443/bfs/1.png',
                               'https://name:pass@i0.hdslb.com/bfs/1.png', '//i0.hdslb.com/bfs/1.png',
                               'file:///bfs/1.png', 'https://i0.hdslb.com/bfs/1.png#x'])
def test_media_endpoint_rejected(url):
    with pytest.raises(probe.Stop): probe.media_descriptor(url, {'num': 1, 'suffix': '', 'extension': 'png'})


def test_seventh_request_and_post_stop_never_reach_transport(native):
    report = run(native)
    assert report['status'] == 'PASS'
    session = requests.Session()
    native['guard'].install(session)
    with pytest.raises(probe.Stop, match='request_budget_exhausted'):
        session.get(probe.NAV, timeout=15)
    native['guard'].count = 0
    with pytest.raises(probe.Stop, match='request_budget_exhausted'):
        session.get(probe.NAV, timeout=15)
    assert len(native['calls']) == 6


@pytest.mark.parametrize('setting', ['timeout', 'retries', 'deadline', 'method'])
def test_invalid_request_settings_stop_before_transport(native, setting):
    session = requests.Session()
    if setting == 'retries': session.adapters['https://'].max_retries.total = 1
    if setting == 'deadline': native['time'].now = 181
    with pytest.raises(probe.Stop):
        native['guard'].install(session)
        session.request('POST' if setting == 'method' else 'GET', probe.NAV, timeout=16 if setting == 'timeout' else 15)
    assert native['calls'] == []


@pytest.mark.parametrize('case', ['gzip', 'empty', 'exact_limit', 'raw_over', 'decoded_over', 'truncated', 'crc',
                                 'concat', 'trailing', 'unknown', 'duplicate_encoding', 'multiple_encoding'])
def test_native_decoding_integrity_and_limits(native, monkeypatch, case):
    body = ('window.__INITIAL_STATE__=' + json.dumps(native['article']) + ';').encode()
    headers = [('Content-Encoding', 'GZiP')]
    encoded = gzip.compress(body)
    if case == 'empty': encoded = gzip.compress(b'')
    if case == 'exact_limit': monkeypatch.setattr(probe, 'PAGE_LIMIT', len(body))
    if case == 'raw_over': monkeypatch.setattr(probe, 'PAGE_LIMIT', len(encoded) - 1)
    if case == 'decoded_over': monkeypatch.setattr(probe, 'PAGE_LIMIT', len(body) - 1)
    if case == 'truncated': encoded = encoded[:-5]
    if case == 'crc': encoded = encoded[:-8] + b'12345678'
    if case == 'concat': encoded += gzip.compress(b'x')
    if case == 'trailing': encoded += b'x'
    if case == 'unknown': headers = [('Content-Encoding', 'br')]
    if case == 'duplicate_encoding': headers += headers
    if case == 'multiple_encoding': headers = [('Content-Encoding', 'gzip, identity')]
    native['responses'][probe.PAGE] = (encoded, 200, headers)
    report = run(native)
    if case in ('gzip', 'exact_limit'):
        assert report['status'] == 'PASS', report
        event = report['requests'][1]
        assert event['raw_sha256'] == hashlib.sha256(encoded).hexdigest()
        assert event['decoded_sha256'] == hashlib.sha256(body).hexdigest()
    else:
        assert report['status'] == 'BLOCKED' and len(native['calls']) == 2, report


@pytest.mark.parametrize('case', ['encoding', 'signature', 'asset_length', 'asset_body', 'total', 'duplicate_length'])
def test_media_restrictions_keep_partial_evidence(native, monkeypatch, case):
    url = native['urls'][0]
    payload, headers = native['media'][url], []
    if case == 'encoding': headers = [('Content-Encoding', 'gzip')]; payload = gzip.compress(payload)
    if case == 'signature': payload = SECRET.encode()
    if case == 'asset_length': headers = [('Content-Length', str(probe.ASSET_LIMIT + 1))]
    if case == 'asset_body': monkeypatch.setattr(probe, 'ASSET_LIMIT', 16)
    if case == 'total': monkeypatch.setattr(probe, 'TOTAL_LIMIT', 16)
    if case == 'duplicate_length': headers = [('Content-Length', '1'), ('Content-Length', '2')]
    native['responses'][url] = (payload, 200, headers)
    report = run(native)
    assert report['status'] == 'BLOCKED' and len(native['calls']) == 3, report
    assert not (native['directory'] / '01.png').exists()
    assert report['partial_files_preserved']


def test_existing_directory_is_not_overwritten(native):
    (native['directory'] / 'keep.txt').write_text('keep')
    assert run(native)['stop_reason'] == 'output_directory_not_empty'
    assert native['calls'] == [] and (native['directory'] / 'keep.txt').read_text() == 'keep'


@pytest.mark.parametrize('suffix', ['../elsewhere', 'other-name', 'gallery-native-ok/subdir'])
def test_output_path_must_be_new_direct_artifacts_child(suffix):
    with pytest.raises(probe.Stop): probe.paths(probe.ROOT / '.artifacts' / suffix)


def test_chunked_gzip_uses_native_decoder_with_both_hashes(monkeypatch):
    body = b'{"code":0,"data":{"isLogin":true,"mid":1}}'
    encoded = gzip.compress(body)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Content-Encoding', 'gzip')
            self.end_headers()
            for chunk in (encoded[:10], encoded[10:]):
                self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
            self.wfile.write(b'0\r\n\r\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    monkeypatch.setattr(probe, 'NAV', f'http://127.0.0.1:{server.server_port}/')
    guard = probe.Guard()
    try:
        guard.install(session)
        assert session.get(probe.NAV, timeout=15).content == body
        assert guard.trace[0]['raw_sha256'] == hashlib.sha256(encoded).hexdigest()
        assert guard.trace[0]['decoded_sha256'] == hashlib.sha256(body).hexdigest()
    finally:
        guard.close(); session.close(); server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_actual_ffmpeg_decodes_all_native_saved_synthetic_pngs(native):
    binary = os.environ.get('BILI_TEST_FFMPEG')
    if not binary: pytest.skip('Set BILI_TEST_FFMPEG for actual synthetic integration')
    import time
    ffmpeg, ffprobe = probe.legacy.check_ffmpeg(Path(binary))
    report = run(native, lambda path, deadline: probe.legacy.inspect_asset(path, 'png', ffmpeg, ffprobe, time.monotonic() + 20))
    assert report['status'] == 'PASS', report
    assert all(a['decoded_frames'] >= 1 for a in report['assets'])


def test_failure_restores_shared_adapters_and_logging(native):
    native['responses'][probe.PAGE] = (b'blocked', 412)
    level = logging.root.manager.disable
    assert run(native)['status'] == 'BLOCKED'
    assert logging.root.manager.disable == level
    native['responses'].clear()
    native['guard'] = probe.Guard(clock=native['time'].clock, sleep=native['time'].sleep)
    assert run(native)['status'] == 'PASS'


def test_decode_failure_keeps_complete_file_but_not_acceptance(native):
    def verifier(path, deadline):
        raise probe.legacy.ProbeStop(SECRET)
    report = run(native, verifier)
    assert report['stop_reason'] == 'local_verification_failed'
    assert report['assets'][0]['download'] == 'COMPLETE_NOT_DECODED'
    assert (native['directory'] / '01.png').is_file()
    assert len(native['calls']) == 3


def test_mid_stream_limit_keeps_part_without_final_file(native, monkeypatch):
    native['media'][native['urls'][0]] = png((1, 2, 3)) + b'x' * 100000
    monkeypatch.setattr(probe, 'ASSET_LIMIT', 40000)
    report = run(native)
    assert report['stop_reason'] == 'asset_byte_limit'
    assert len(native['calls']) == 3
    assert (native['directory'] / '01.png.part').is_file()
    assert not (native['directory'] / '01.png').exists()


def test_real_length_truncation_stops_native_reader(native):
    url = native['urls'][0]
    body = native['media'][url]
    native['responses'][url] = (body, 200, [('Content-Length', str(len(body) + 1))])
    report = run(native)
    assert report['stop_reason'] == 'transport_read_failed'
    assert len(native['calls']) == 3


def test_worker_requires_start_token_before_any_preflight(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'stdin', io.StringIO('WRONG\n'))
    monkeypatch.setattr(probe, 'fingerprints', lambda *args: pytest.fail('preflight must not run'))
    assert probe.main(['--worker', '--profile', 'unused', '--baseline-report', str(tmp_path / 'baseline'),
                       '--ffmpeg', str(tmp_path / 'ffmpeg.exe'), '--output', str(tmp_path / 'out')]) == 2


def test_changed_fingerprint_prevents_cookie_access(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, 'stdin', io.StringIO(probe.START + '{"fingerprints":{}}\n'))
    monkeypatch.setattr(probe, 'fingerprints', lambda *args: {'changed': True})
    monkeypatch.setattr(probe, 'load_bili_cookies', lambda *args: pytest.fail('cookies must not be read'))
    assert probe.main(['--worker', '--profile', 'unused', '--baseline-report', str(tmp_path / 'baseline'),
                       '--ffmpeg', str(tmp_path / 'ffmpeg.exe'), '--output', str(tmp_path / 'out')]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['stop_reason'] == 'preflight_fingerprint_changed' and result['network_requests'] == 0


def test_parent_timeout_closes_job_and_reports_unknown_count(monkeypatch, tmp_path):
    from types import SimpleNamespace
    state = {'closed': 0, 'calls': 0}
    class Job:
        def assign(self, process): state['assigned'] = True
        def close(self): state['closed'] += 1
    class Process:
        returncode = 0
        def __init__(self, *args, **kwargs):
            assert kwargs['stderr'] == probe.subprocess.DEVNULL
        def communicate(self, *args, **kwargs):
            state['calls'] += 1
            if state['calls'] == 1:
                assert state['assigned'] and kwargs['timeout'] == 180
                raise probe.subprocess.TimeoutExpired('synthetic', 180)
            return '', None
        def poll(self): return 0
    monkeypatch.setattr(probe.legacy, 'WindowsJob', Job)
    monkeypatch.setattr(probe.subprocess, 'Popen', Process)
    result = probe.bounded(SimpleNamespace(profile='unused', baseline_report=tmp_path / 'baseline',
                                          ffmpeg=tmp_path / 'ffmpeg.exe', output=tmp_path / 'out'), {})
    assert result['stop_reason'] == 'worker_total_timeout'
    assert result['network_requests'] is None and state['closed'] >= 1
