"""Fixed image/livephoto research spike around gallery-dl's native pipeline.

No product imports this module. Transport observers call the original adapter,
raw readers and decoder; the upstream job owns filenames, .part and publication.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

import gallery_download_probe as legacy
from gallery_page_probe import audit_state, original_modules
from research_probe import load_bili_cookies

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = '1249456774436618276'
PAGE = 'https://www.bilibili.com/opus/' + SAMPLE
NAV = 'https://api.bilibili.com/x/web-interface/nav'
REQUEST_LIMIT = 6
PAGE_LIMIT = 3 * 1024 * 1024
ASSET_LIMIT = 64 * 1024 * 1024
TOTAL_LIMIT = 256 * 1024 * 1024
WORKER_SECONDS = 180
START = 'START_NATIVE_GALLERY_01\n'
PINS = {'gallery-dl': '1.32.16', 'requests': '2.34.2', 'urllib3': '2.8.0'}
SOURCE_FILES = ('experiments/gallery_native_probe.py', 'experiments/gallery_download_probe.py',
                'experiments/gallery_page_probe.py', 'experiments/video_download_probe.py',
                'experiments/research_probe.py', 'experiments/gallery_probe.py',
                'tests/test_gallery_native_probe.py', 'requirements-research.lock.txt')
LIVE_BASELINE_SHA256 = 'a98aaf48db64d4c85bc5baeaf7ccc59a0c609d699d60b34cdcc345ded8dd3b5b'
LOCAL_FAILURES = frozenset({
    'worker_total_timeout', 'local_probe_failed', 'invalid_local_streams', 'unexpected_local_codec',
    'invalid_local_dimensions', 'original_format_mismatch', 'local_decode_failed',
    'local_decode_summary_limit', 'local_decode_summary_missing', 'insufficient_decoded_frames',
    'missing_video_duration', 'local_decode_or_probe_timeout', 'local_validation_error',
})


@dataclass(frozen=True)
class Sample:
    kind: str
    dynamic_id: str
    images: int
    image_format: str
    live: bool

    @property
    def page(self):
        return 'https://www.bilibili.com/opus/' + self.dynamic_id

    @property
    def assets(self):
        return self.images * (2 if self.live else 1)

    @property
    def requests(self):
        return 2 + self.assets

    @property
    def experiment(self):
        return 'NATIVE_LIVEPHOTO_01' if self.live else 'NATIVE_GALLERY_01'


SAMPLES = {'images': Sample('images', SAMPLE, 4, 'png', False),
           'livephoto': Sample('livephoto', '1257912749013336118', 9, 'jpg', True)}


def selected_sample(kind):
    try:
        return SAMPLES[kind]
    except (KeyError, TypeError):
        raise Stop('sample_not_preregistered') from None


def load_baseline(path, sample):
    baseline = legacy.load_baseline(path, sample.dynamic_id)
    if sample.live and baseline['sha256'] != LIVE_BASELINE_SHA256:
        raise Stop('fixed_baseline_fingerprint_mismatch')
    return baseline


class Stop(BaseException):
    """Bypass upstream's catch-and-continue Exception paths, then stop worker."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def media_descriptor(url, metadata, sample=SAMPLES['images']):
    """Preserve original HTTP/HTTPS URLs; never upgrade or invent an endpoint."""
    try:
        if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 for c in url):
            raise ValueError
        p = urlsplit(url)
        extension, suffix = metadata.get('extension'), metadata.get('suffix')
        video = sample.live and suffix == 'l' and extension == 'mp4'
        host = p.hostname or ''
        video_host = (video and any(host.endswith('.' + ending) for ending in legacy.VIDEO_CDN_SUFFIXES)
                      and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                              for label in host.split('.')))
        if (p.scheme not in ('http', 'https') or not (host in legacy.CDN_HOSTS or video_host)
                or p.username is not None or p.password is not None or p.fragment
                or p.port not in (None, 80 if p.scheme == 'http' else 443)
                or (not video and not p.path.startswith('/bfs/'))
                or not p.path.endswith('.mp4' if video else '.' + sample.image_format)):
            raise ValueError
        if (type(metadata.get('num')) is not int or not 1 <= metadata['num'] <= sample.images
                or not (video or (suffix == '' and extension == sample.image_format))):
            raise ValueError
        return {'sequence': metadata['num'], 'suffix': suffix, 'format': extension,
                'source_path_sha256': hashlib.sha256(p.path.encode()).hexdigest()}
    except (ValueError, TypeError, AttributeError):
        raise Stop('media_endpoint_or_format_rejected') from None


class Guard:
    def __init__(self, *, sample='images', clock=time.monotonic, sleep=time.sleep):
        self.sample = selected_sample(sample)
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.deadline = self.started + WORKER_SECONDS
        self.last = None
        self.count = self.media_count = self.total_media = 0
        self.account_ok = False
        self.allowed = []
        self.stopped = None
        self.trace = []
        self.responses = []
        self.adapters = []

    def stop(self, reason):
        if self.stopped is None:
            self.stopped = reason
        raise Stop(self.stopped)

    def remaining(self):
        if self.stopped:
            raise Stop(self.stopped)
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            self.stop('worker_total_timeout')
        return remaining

    def kind(self, request):
        if request.method != 'GET' or request.body is not None:
            self.stop('unexpected_request_method_or_body')
        if self.count == 0 and request.url == NAV:
            return 'account'
        if self.count == 1 and self.account_ok and request.url == self.sample.page:
            return 'page'
        if (self.account_ok and len(self.allowed) == self.sample.assets and self.count >= 2
                and self.media_count < self.sample.assets and request.url == self.allowed[self.media_count]):
            if any(name.lower() in ('cookie', 'authorization', 'proxy-authorization') for name in request.headers):
                self.stop('media_sensitive_header_rejected')
            return 'media'
        self.stop('endpoint_or_sequence_rejected')

    def install(self, session):
        """Observe the native adapters, preserving request/proxy/TLS arguments."""
        for adapter in {id(a): a for a in session.adapters.values()}.values():
            if adapter.max_retries.total != 0:
                self.stop('native_transport_retry_not_zero')
            original = adapter.send
            self.adapters.append((adapter, 'send' in adapter.__dict__, adapter.__dict__.get('send')))

            def send(request, *args, _original=original, **kwargs):
                self.remaining()
                if self.count >= self.sample.requests:
                    self.stop('request_budget_exhausted')
                kind = self.kind(request)
                timeout = kwargs.get('timeout')
                if not isinstance(timeout, (int, float)) or not 0 < timeout <= 15:
                    self.stop('native_timeout_not_bounded')
                delay = 0 if self.last is None else max(0, 1 - (self.clock() - self.last))
                if delay >= self.remaining():
                    self.stop('worker_total_timeout')
                self.sleep(delay)
                self.remaining()
                self.count += 1
                self.media_count += kind == 'media'
                self.last = self.clock()
                event = {'number': self.count, 'kind': kind, 'status': None,
                         'started_seconds': round(self.last - self.started, 6)}
                self.trace.append(event)
                response = None
                try:
                    response = _original(request, *args, **kwargs)
                    self.responses.append(response)
                    event['status'] = int(response.status_code)
                    if response.status_code != 200:
                        self.stop('redirect_rejected' if 300 <= response.status_code < 400 else 'http_status_stop')
                    self.observe_response(response, kind, event)
                    return response
                except Stop:
                    if response is not None:
                        response.close()
                    raise
                except Exception:
                    if response is not None:
                        response.close()
                    self.stop('transport_failed')

            adapter.send = send

    def observe_response(self, response, kind, event):
        limit = ASSET_LIMIT if kind == 'media' else PAGE_LIMIT
        encoding = response.raw.headers.getlist('Content-Encoding')
        if encoding and (len(encoding) != 1 or encoding[0].strip().lower() not in
                         (('identity',) if kind == 'media' else ('identity', 'gzip'))):
            self.stop('content_encoding_rejected')
        lengths = response.raw.headers.getlist('Content-Length')
        if lengths and (len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0])):
            self.stop('content_length_rejected')
        if lengths and int(lengths[0]) > limit:
            self.stop('asset_byte_limit' if kind == 'media' else 'page_raw_byte_limit')
        if kind == 'media' and lengths and self.total_media + int(lengths[0]) > TOTAL_LIMIT:
            self.stop('sample_byte_limit')
        raw, raw_bytes, decoded_bytes = response.raw, 0, 0
        raw_hash, decoded_hash = hashlib.sha256(), hashlib.sha256()
        raw_body = bytearray()
        prefix = bytearray()
        # The selected manifest is already validated before any media dispatch.
        signature = (b'\x89PNG\r\n\x1a\n' if not self.sample.live else
                     b'\xff\xd8\xff' if self.media_count % 2 == 1 else None)
        tail = b''
        event.update(raw_bytes=0, decoded_bytes=0, complete=False)

        def observe_raw(data):
            nonlocal raw_bytes
            self.remaining()
            if not data:
                return data
            raw_bytes += len(data)
            event['raw_bytes'] = raw_bytes
            raw_hash.update(data)
            if raw_bytes > limit:
                self.stop('asset_byte_limit' if kind == 'media' else 'page_raw_byte_limit')
            if kind != 'media':
                raw_body.extend(data)
            return data

        # Pin-specific taps cover both Content-Length and chunked bodies. They
        # return exactly the upstream bytes; native urllib3 still decodes them.
        for name in ('_raw_read', '_handle_chunk'):
            original = getattr(raw, name)
            def read(*args, _original=original, **kwargs):
                try:
                    return observe_raw(_original(*args, **kwargs))
                except Stop:
                    raise
                except Exception:
                    self.stop('transport_read_failed')
            setattr(raw, name, read)
        original_stream = raw.stream

        def stream(*args, **kwargs):
            nonlocal decoded_bytes, tail
            try:
                for data in original_stream(*args, **kwargs):
                    self.remaining()
                    decoded_bytes += len(data)
                    event['decoded_bytes'] = decoded_bytes
                    if decoded_bytes > limit:
                        self.stop('asset_byte_limit' if kind == 'media' else 'page_decoded_byte_limit')
                    decoded_hash.update(data)
                    if kind == 'media':
                        self.total_media += len(data)
                        if self.total_media > TOTAL_LIMIT:
                            self.stop('sample_byte_limit')
                        if signature is not None:
                            prefix.extend(data[:max(0, len(signature) - len(prefix))])
                            if len(prefix) == len(signature) and prefix != signature:
                                self.stop('media_signature_rejected')
                    else:
                        scan = tail + data
                        if b'window._riskdata_' in scan or b'window.__riskdata__' in scan:
                            self.stop('verification_page_detected')
                        tail = scan[-32:]
                    yield data
                if kind != 'media':
                    try:
                        validated, _, decoding = legacy.shared_process.decode_metadata(bytes(raw_body), response.raw.headers)
                    except legacy.shared_process.ProbeStop:
                        self.stop('metadata_encoding_integrity_rejected')
                    if hashlib.sha256(validated).hexdigest() != decoded_hash.hexdigest():
                        self.stop('native_decode_validation_mismatch')
                    event['decoding_status'] = decoding['status']
                event.update(complete=True, raw_sha256=raw_hash.hexdigest(), decoded_sha256=decoded_hash.hexdigest())
            except Stop:
                response.close()
                raise
            except Exception:
                response.close()
                self.stop('transport_read_failed')
        raw.stream = stream

    def close(self):
        for response in self.responses:
            response.close()
        # gallery-dl caches adapters across extractor instances in this process.
        # Restore their prior instance state, including class method lookup.
        for adapter, existed, value in reversed(self.adapters):
            if existed:
                adapter.send = value
            else:
                del adapter.send
        self.adapters.clear()

    def summary(self):
        return {'network_requests': self.count, 'media_requests': self.media_count,
                'total_media_bytes': self.total_media, 'requests': self.trace,
                'elapsed_seconds': round(self.clock() - self.started, 6),
                'account': 'PASS' if self.account_ok else 'NOT_PASSED'}


@contextlib.contextmanager
def native_configuration(directory, sample=SAMPLES['images']):
    from gallery_dl import config
    previous = copy.deepcopy(config._config)
    logging_level = logging.root.manager.disable
    config.clear()  # Embedded API does not load user's gallery-dl config files.
    for key, value in {'base-directory': str(directory), 'directory': [], 'filename': '{num:02}{suffix}.{extension}',
                       'retries': 0, 'timeout': 15, 'sleep-request': 0, 'sleep': 0,
                       'fallback': False, 'postprocessors': [], 'skip': True, 'input': False,
                       'write-pages': False, 'metadata-url': None, 'archive': None}.items():
        config.set(('extractor',), key, value)
    if sample.live:
        config.set(('extractor', 'bilibili'), 'livephoto', True)
    for key, value in {'retries': 0, 'timeout': 15, 'adjust-extensions': False,
                       'part': True, 'progress': 0, 'consume-content': False}.items():
        config.set(('downloader',), key, value)
    config.set(('output',), 'mode', 'null')
    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            yield
    finally:
        logging.disable(logging_level)
        config.clear()
        config._config.update(previous)


def validate_article(article, report, sample=SAMPLES['images']):
    if not isinstance(article, dict) or type(article.get('id')) not in (str, int) or str(article['id']) != sample.dynamic_id:
        raise Stop('work_identity_mismatch')
    structure = original_modules(article)
    report['original_modules'] = structure
    if structure['status'] != 'OBSERVED':
        raise Stop('original_modules_invalid')
    for field in structure['blocked_fields']:
        if field['truthy']:
            raise Stop('effective_blocked_module')
        if field['value_type'] not in ('NULL', 'OBJECT'):
            raise Stop('blocked_module_type_invalid')
    authors = [m['module_author'] for m in article['detail']['modules'] if m.get('module_author')]
    if not authors or any(not isinstance(a, dict) or type(a.get('mid')) not in (int, str)
                          or str(a['mid']) != legacy.AUTHOR_UID for a in authors):
        raise Stop('author_identity_mismatch')


def validated_messages(extractor, guard, baseline, report):
    from gallery_dl.extractor.common import Message
    original = extractor.items
    sample = guard.sample

    def messages():
        buffered, rows, urls = [], [], []
        for kind, url, data in original():
            if len(buffered) >= sample.assets + 1 or not isinstance(data, dict):
                raise Stop('message_contract_changed')
            data = copy.deepcopy(data)  # Upstream mutates the same dict for each picture.
            if (str(data.get('id')) != sample.dynamic_id or str(data.get('user_id')) != legacy.AUTHOR_UID
                    or any(k.startswith('_http') for k in data)):
                raise Stop('message_identity_or_control_mismatch')
            if kind == Message.Directory and not buffered:
                if data.get('count') != sample.images or type(data.get('count')) is not int:
                    raise Stop('image_count_mismatch')
            elif kind == Message.Url and buffered:
                rows.append(media_descriptor(url, data, sample))
                urls.append(url)
            else:
                raise Stop('message_contract_changed')
            buffered.append((kind, url, data))
        if (len(buffered) != sample.assets + 1 or len(set(urls)) != sample.assets or rows != baseline['assets']
                or baseline['author_uid'] != legacy.AUTHOR_UID):
            raise Stop('fixed_sample_baseline_mismatch')
        guard.allowed = urls
        report.update(baseline_match='PASS', author_uid=legacy.AUTHOR_UID, declared_images=sample.images)
        yield from buffered
    extractor.items = messages


def execute(directory, baseline, jar, verifier, *, guard=None, sample='images'):
    """Run a whole native job once. verifier is local-only and precedes next URL."""
    from gallery_dl import job
    from gallery_dl.extractor.bilibili import BilibiliArticleExtractor
    guard = guard or Guard(sample=sample)
    sample = selected_sample(sample)
    report = {'schema_version': 2, 'experiment': sample.experiment, 'sample_kind': sample.kind,
              'sample_dynamic_id': sample.dynamic_id,
              'status': 'RUNNING', 'assets': [], 'baseline_match': 'NOT_CHECKED',
              'research_gate': 'NEEDS_MORE_EVIDENCE', 'manual_image_preview': 'NOT_TESTED',
              'manual_live_playback': 'NOT_TESTED',
              'pairs': [{'sequence': i, 'status': 'NOT_STARTED', 'image_filename': f'{i:02}.jpg',
                         'video_filename': f'{i:02}l.mp4'} for i in range(1, sample.images + 1)] if sample.live else []}
    extractor = None
    with native_configuration(directory, sample):
        try:
            if guard.sample != sample:
                raise Stop('guard_sample_mismatch')
            if any(directory.iterdir()):
                raise Stop('output_directory_not_empty')
            extractor = BilibiliArticleExtractor.from_url(sample.page)
            extractor.initialize()
            extractor.session.cookies.update(jar)
            guard.install(extractor.session)
            nav = extractor.request(NAV).json()
            data = nav.get('data')
            if (nav.get('code') != 0 or not isinstance(data, dict) or data.get('isLogin') is not True
                    or type(data.get('mid')) is not int or data['mid'] <= 0):
                raise Stop('account_not_logged_in_or_invalid')
            cookie_ids = [c.value for c in jar if c.name == 'DedeUserID']
            if cookie_ids and any(value != str(data['mid']) for value in cookie_ids):
                raise Stop('account_cookie_identity_mismatch')
            guard.account_ok = True

            original_request = extractor.request
            def request(url, *args, **kwargs):
                if url != sample.page:
                    raise Stop('unexpected_source_endpoint')
                response = original_request(url, *args, **kwargs)
                try:
                    page = response.content.decode('utf-8', errors='strict')
                except UnicodeError:
                    raise Stop('page_utf8_invalid') from None
                audit = audit_state(page)
                report['state_structure'] = {k: audit[k] for k in ('status', 'assignment_count', 'duplicate_member_count')}
                if audit['status'] != 'PARSED' or audit['assignment_count'] != 1 or audit['duplicate_member_count']:
                    raise Stop('page_state_ambiguous_or_invalid')
                return response
            extractor.request = request
            original_article = extractor.api.article
            def article(article_id):
                value = original_article(article_id)
                validate_article(value, report, sample)
                return value
            extractor.api.article = article
            validated_messages(extractor, guard, baseline, report)

            class WholeTaskJob(job.DownloadJob):
                def handle_url(self, url, data):
                    row = {**media_descriptor(url, data, sample),
                           'filename': f"{data['num']:02}{data['suffix']}.{data['extension']}",
                           'pair_role': 'live_video' if data['suffix'] == 'l' else 'image', 'download': 'IN_PROGRESS'}
                    report['assets'].append(row)
                    if sample.live:
                        report['pairs'][row['sequence'] - 1]['status'] = 'INCOMPLETE'
                    super().handle_url(url, data)
                    if self.status:
                        guard.stop('native_download_failed')
                    path = directory / row['filename']
                    if (not path.is_file() or path.is_symlink() or path.is_junction()
                            or not 0 < path.stat().st_size <= ASSET_LIMIT):
                        guard.stop('local_file_missing_or_invalid')
                    event = guard.trace[-1]
                    if (event['kind'] != 'media' or not event['complete']
                            or event['decoded_bytes'] != path.stat().st_size or event['decoded_sha256'] != digest(path)):
                        guard.stop('local_file_transport_mismatch')
                    row.update(size=path.stat().st_size, sha256=digest(path), download='COMPLETE_NOT_DECODED')
                    try:
                        row.update(verifier(path, guard.deadline))
                    except legacy.ProbeStop as exc:
                        reason = exc.reason if exc.reason in LOCAL_FAILURES else 'local_verification_failed'
                        row.update(decode='FAIL', failure_reason=reason)
                        guard.stop(reason)
                    except Exception:
                        row.update(decode='FAIL', failure_reason='local_verification_failed')
                        guard.stop('local_verification_failed')
                    if row.get('decode') != 'PASS':
                        guard.stop('local_decode_failed')
                    row['download'] = 'PASS'
                    if sample.live and row['pair_role'] == 'live_video':
                        paired = [a for a in report['assets'] if a['sequence'] == row['sequence']]
                        if len(paired) == 2 and all(a['download'] == 'PASS' for a in paired):
                            report['pairs'][row['sequence'] - 1]['status'] = 'PASS'

            task = WholeTaskJob(extractor)
            task.run()
            if (task.status or len(report['assets']) != sample.assets or guard.count != sample.requests
                    or any(row['download'] != 'PASS' for row in report['assets'])
                    or any(pair['status'] != 'PASS' for pair in report['pairs'])):
                raise Stop('native_job_incomplete')
            report['status'] = 'PASS'
        except Stop as exc:
            report.update(status='BLOCKED', stop_reason=exc.reason)
        except legacy.ProbeStop:
            report.update(status='BLOCKED', stop_reason='local_verification_failed')
        except Exception:
            report.update(status='BLOCKED', stop_reason='native_pipeline_failed')
        finally:
            guard.close()
            if extractor is not None:
                extractor.session.close()
            report.update(guard.summary(), partial_files_preserved=True)
    return report


def paths(output):
    """A new direct child only; reject every reparse ancestor before resolving."""
    candidate = Path(os.path.abspath(output))
    base = ROOT / '.artifacts'
    for p in (ROOT, base, candidate):
        if p.is_symlink() or p.is_junction():
            raise Stop('output_reparse_rejected')
    if candidate.parent != base or not re.fullmatch(r'gallery-native-[A-Za-z0-9_-]{1,64}', candidate.name):
        raise Stop('new_direct_artifacts_directory_required')
    return candidate


def fingerprints(ffmpeg, baseline_path):
    dependencies = {name: importlib.metadata.version(name) for name in PINS}
    if dependencies != PINS:
        raise Stop('dependency_version_mismatch')
    # Pin the actual observer-dependent upstream implementations as well as versions.
    import gallery_dl.job, gallery_dl.downloader.http, gallery_dl.extractor.bilibili
    import requests.adapters, requests.sessions, urllib3.response
    modules = (gallery_dl.job, gallery_dl.downloader.http, gallery_dl.extractor.bilibili,
               requests.adapters, requests.sessions, urllib3.response)
    return {'source_sha256': {name: digest(ROOT / name) for name in SOURCE_FILES},
            'dependencies': dependencies, 'dependency_source_sha256': {m.__name__: digest(m.__file__) for m in modules},
            'baseline_report_sha256': digest(baseline_path),
            'ffmpeg_sha256': digest(ffmpeg), 'ffprobe_sha256': digest(ffmpeg.with_name('ffprobe.exe'))}


def bounded(args, registry):
    process = None
    job = legacy.WindowsJob()
    try:
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--profile', args.profile,
                   '--baseline-report', str(args.baseline_report), '--ffmpeg', str(args.ffmpeg), '--output', str(args.output)]
        command.extend(['--sample', getattr(args, 'sample', 'images')])
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job.assign(process)
        try:
            stdout, _ = process.communicate(START + json.dumps(registry) + '\n', timeout=WORKER_SECONDS)
        except subprocess.TimeoutExpired:
            job.close()
            process.communicate(timeout=5)
            return {'status': 'BLOCKED', 'stop_reason': 'worker_total_timeout', 'network_requests': None,
                    'request_count_status': 'UNKNOWN_AFTER_TIMEOUT', 'partial_files_preserved': True}
        if process.returncode or len(stdout) > 128 * 1024:
            raise Stop('worker_protocol_failed')
        value = json.loads(stdout)
        if not isinstance(value, dict) or value.get('status') not in ('PASS', 'BLOCKED'):
            raise Stop('worker_protocol_failed')
        return value
    finally:
        job.close()
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--baseline-report', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sample', choices=tuple(SAMPLES), default='images')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sample = selected_sample(args.sample)
    if args.worker:
        if sys.stdin.readline() != START:
            return 2
        result = {'status': 'BLOCKED', 'stop_reason': 'worker_preflight_failed', 'network_requests': 0}
        with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            try:
                registry = json.loads(sys.stdin.readline(128 * 1024))
                if fingerprints(args.ffmpeg, args.baseline_report) != registry['fingerprints']:
                    raise Stop('preflight_fingerprint_changed')
                if (registry.get('sample_kind') != sample.kind or registry.get('sample') != sample.dynamic_id
                        or registry.get('request_limit') != sample.requests):
                    raise Stop('preregistration_sample_mismatch')
                output = paths(args.output)
                baseline = load_baseline(args.baseline_report, sample)
                ffmpeg, ffprobe = legacy.check_ffmpeg(args.ffmpeg)
                jar, _ = load_bili_cookies(args.profile, None)
                result = execute(output / 'media', baseline, jar,
                                 lambda path, deadline: legacy.inspect_asset(path, path.suffix[1:], ffmpeg, ffprobe, deadline),
                                 sample=sample.kind)
            except Stop as exc:
                result['stop_reason'] = exc.reason
            except Exception:
                pass  # No raw exception, URL, account or upstream log crosses stdout.
        print(json.dumps(result, ensure_ascii=True))
        return 0
    try:
        args.output = paths(args.output)
        load_baseline(args.baseline_report, sample)
        args.ffmpeg, _ = legacy.check_ffmpeg(args.ffmpeg)
        registry = {'experiment': sample.experiment, 'sample_kind': sample.kind,
                    'registered_utc': datetime.now(timezone.utc).isoformat(),
                    'sample': sample.dynamic_id, 'author_uid': legacy.AUTHOR_UID, 'expected_images': sample.images,
                    'expected_assets': sample.assets,
                    'request_limit': sample.requests, 'retries': 0, 'redirects': 0, 'worker_seconds': WORKER_SECONDS,
                    'network_wait_seconds': 15, 'interval_seconds': 1, 'max_asset_bytes': ASSET_LIMIT,
                    'max_total_bytes': TOTAL_LIMIT, 'max_raw_and_decoded_page_bytes': PAGE_LIMIT,
                    'media_schemes': ['http', 'https'], 'media_hosts': sorted(legacy.CDN_HOSTS),
                    'video_cdn_subdomains': list(legacy.VIDEO_CDN_SUFFIXES) if sample.live else [],
                    'fingerprints': fingerprints(args.ffmpeg, args.baseline_report)}
        args.output.mkdir(exist_ok=False)
        (args.output / 'media').mkdir()
        with (args.output / 'registration.json').open('x', encoding='utf-8') as f:
            json.dump(registry, f, indent=2)
    except (Exception, Stop):
        parser.error('Requires pinned dependencies, valid baseline/tools and a new direct .artifacts/gallery-native-NAME directory')
    report = {'schema_version': 2, 'sample_kind': sample.kind, 'status': 'RUNNING',
              'registration_sha256': digest(args.output / 'registration.json'),
              'research_gate': 'NEEDS_MORE_EVIDENCE', 'credentials_persisted': False, 'raw_logs_persisted': False}
    with (args.output / 'report.json').open('x', encoding='utf-8') as f:
        json.dump(report, f); f.flush()
        try:
            report.update(bounded(args, registry))
        except (Exception, Stop):
            report.update(status='BLOCKED', stop_reason='parent_or_worker_failed', network_requests=None)
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        try:
            report['fingerprints_unchanged'] = fingerprints(args.ffmpeg, args.baseline_report) == registry['fingerprints']
        except (Exception, Stop):
            report['fingerprints_unchanged'] = False
        if not report['fingerprints_unchanged']:
            report.update(status='BLOCKED', stop_reason='fingerprint_changed_during_run')
        f.seek(0); json.dump(report, f, indent=2); f.truncate()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
