"""Bounded observers adapted from verified probes; native requests/urllib3 own IO."""
import contextlib
import copy
from email.message import Message
import hashlib
import http.cookiejar
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace
import zlib
MAX_METADATA_BYTES=PAGE_LIMIT=3*1024*1024
ASSET_LIMIT=64*1024*1024
TOTAL_LIMIT=256*1024*1024
WORKER_SECONDS=180
NAV='https://api.bilibili.com/x/web-interface/nav'
ASSIGNMENT='window.__INITIAL_STATE__='
MARKER='module_blocked'
class Stop(BaseException):
 def __init__(self,reason,evidence=None):
  self.reason=reason; self.evidence=evidence or {}; super().__init__(reason)
ProbeStop=Stop

def decode_metadata(body, headers):
    """Decode one bounded gzip member; no guessing, trailing data or unbounded flush."""
    if len(body) > MAX_METADATA_BYTES:
        raise ProbeStop("metadata_size_limit")
    values = (headers.get_all("Content-Encoding", []) if hasattr(headers, "get_all") else
              [value for key, value in headers.items() if key.lower() == "content-encoding"])
    encoding = "MISSING"
    if values:
        value = values[0].strip(" \t").lower() if len(values) == 1 and isinstance(values[0], str) else None
        encoding = {"identity": "IDENTITY", "gzip": "GZIP"}.get(value, "UNSUPPORTED")
        if encoding == "UNSUPPORTED":
            raise ProbeStop("content_encoding_unsupported", {"encoding": encoding})
    decoded = body
    if encoding == "GZIP":
        try:
            decoder = zlib.decompressobj(31)
            decoded = decoder.decompress(body, MAX_METADATA_BYTES + 1)
        except zlib.error:
            raise ProbeStop("metadata_gzip_invalid", {"encoding": encoding}) from None
        if len(decoded) > MAX_METADATA_BYTES:
            raise ProbeStop("metadata_decoded_size_limit", {"encoding": encoding})
        if not decoder.eof or decoder.unconsumed_tail:
            raise ProbeStop("metadata_gzip_incomplete", {"encoding": encoding})
        if decoder.unused_data:
            raise ProbeStop("metadata_gzip_trailing_data", {"encoding": encoding})
    copied = Message()
    for key, value in headers.items():
        if encoding != "GZIP" or key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}:
            copied.add_header(key, value)
    if encoding == "GZIP":
        copied["Content-Length"] = str(len(decoded))
    evidence = {"status": "DECODED" if encoding == "GZIP" else "PASS_THROUGH", "encoding": encoding,
                "raw_bytes": len(body), "decoded_bytes": len(decoded),
                "raw_sha256": hashlib.sha256(body).hexdigest(),
                "decoded_sha256": hashlib.sha256(decoded).hexdigest()}
    return decoded, copied, evidence
def typed(value, present=True):
    kind = ("MISSING" if not present else "NULL" if value is None else "BOOLEAN" if isinstance(value, bool)
            else "OBJECT" if isinstance(value, dict) else "ARRAY" if isinstance(value, list)
            else "STRING" if isinstance(value, str) else "NUMBER" if isinstance(value, (int, float)) else "OTHER")
    return {"key_present": present, "value_type": kind, "truthy": bool(value) if present else False}

class Pairs(dict):
    """Preserve duplicate JSON members for observation, never replace upstream parsing."""
    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = pairs

def audit_state(page):
    # Select exactly the slice used by the pinned upstream BilibiliAPI.article.
    from gallery_dl import text
    result = {"status": "NOT_PARSED", "whole_page_literal_count": page.count('"module_blocked"'),
              "assignment_count": page.count(ASSIGNMENT), "selected_state_literal_count": None,
              "outside_selected_state_literal_count": None, "key_occurrences": [],
              "string_value_marker_count": 0, "duplicate_member_count": 0,
              "duplicate_blocked_member_count": 0}
    try:
        first = page.index(ASSIGNMENT) + len(ASSIGNMENT)
        last = page.index("};", first) + 1
        selected = text.extr(page, ASSIGNMENT, "};") + "}"
        result["selected_state_literal_count"] = selected.count('"module_blocked"')
        result["outside_selected_state_literal_count"] = (page[:first] + page[last:]).count('"module_blocked"')
        state = json.loads(selected, object_pairs_hook=Pairs)
    except (ValueError, RecursionError):
        result["status"] = "PARSE_FAILED"
        return result
    stack, nodes = [(state, (), 0)], 0
    while stack:
        value, path, depth = stack.pop()
        nodes += 1
        if nodes > 20000 or depth > 64 or len(result["key_occurrences"]) > 128:
            result["status"] = "DIAGNOSTIC_LIMIT"
            return result
        if isinstance(value, Pairs):
            seen = set()
            for key, child in value.pairs:
                duplicate = key in seen
                seen.add(key)
                result["duplicate_member_count"] += int(duplicate)
                if key == MARKER:
                    module_field = len(path) == 3 and path[:2] == ("detail", "modules") and type(path[2]) is int
                    result["key_occurrences"].append({"location": "MODULE_FIELD" if module_field else "STATE_OTHER_FIELD",
                        "module_index": path[2] if module_field else None, "duplicate_in_object": duplicate, **typed(child)})
                    result["duplicate_blocked_member_count"] += int(duplicate)
                stack.append((child, path + (key,), depth + 1))
        elif isinstance(value, list):
            stack.extend((child, path + (index,), depth + 1) for index, child in enumerate(value))
        elif isinstance(value, str):
            result["string_value_marker_count"] += value.count(MARKER)
    result["status"] = "PARSED"
    return result

def original_modules(article):
    detail = article.get("detail") if isinstance(article, dict) else None
    modules = detail.get("modules") if isinstance(detail, dict) else None
    result = {"status": "INVALID", "module_count": None, "blocked_fields": [], "merge_overwrite_count": 0}
    if not isinstance(modules, list) or len(modules) > 256 or any(not isinstance(m, dict) for m in modules):
        return result
    result.update(status="OBSERVED", module_count=len(modules))
    for index, module in enumerate(modules):
        if MARKER in module:
            result["blocked_fields"].append({"module_index": index, **typed(module[MARKER])})
    result["merge_overwrite_count"] = max(0, len(result["blocked_fields"]) - 1)
    return result
class QuietLogger:
    """Discard upstream messages: they can contain private paths and data."""
    def __init__(self):
        self.warning_count = 0
        self.error_count = 0

    def debug(self, message, *args, **kwargs):
        pass

    info = debug

    def warning(self, message, *args, **kwargs):
        self.warning_count += 1

    def error(self, message, *args, **kwargs):
        self.error_count += 1

def load_bili_cookies(profile: str | None, cookie_file: str | None):
    logger = QuietLogger()
    try:
        if profile:
            from yt_dlp.cookies import extract_cookies_from_browser
            source = extract_cookies_from_browser("chrome", profile, logger=logger)
        else:
            source = http.cookiejar.MozillaCookieJar(cookie_file)
            source.load(ignore_discard=True, ignore_expires=False)
    except Exception as exc:
        raise ProbeStop("cookie_load_failed", {
            "exception_type": type(exc).__name__,
            "warning_count": logger.warning_count,
            "error_count": logger.error_count,
        }) from None
    jar = http.cookiejar.CookieJar()
    for cookie in source:
        domain = cookie.domain.lstrip(".").lower()
        if (domain == "bilibili.com" or domain.endswith(".bilibili.com")) and not cookie.is_expired():
            jar.set_cookie(cookie)
    evidence = {"bilibili_cookie_count": len(jar), "warning_count": logger.warning_count,
                "error_count": logger.error_count}
    if not any(c.name == "SESSDATA" and c.value for c in jar):
        raise ProbeStop("session_cookie_unavailable", evidence)
    return jar, evidence
def inspect_asset(path, extension, ffmpeg, ffprobe, deadline):
    demuxer = "mov" if extension == "mp4" else "image2"
    def timeout():
        left = deadline - time.monotonic()
        if left <= 0:
            raise ProbeStop("worker_total_timeout")
        return min(20.0, left)
    common = ["-v", "error", "-protocol_whitelist", "file,pipe", "-f", demuxer]
    try:
        result = subprocess.run([str(ffprobe), *common, "-show_entries",
                    "stream=codec_type,codec_name,width,height:format=format_name,duration", "-of", "json", str(path)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", timeout=timeout(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or len(result.stdout) > 128 * 1024:
            raise ProbeStop("local_probe_failed")
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not isinstance(streams, list) or not 1 <= len(streams) <= 8:
            raise ProbeStop("invalid_local_streams")
        safe_streams = []
        allowed_codecs = {"png", "mjpeg", "h264", "hevc", "av1", "aac", "mp3", "alac"}
        for stream in streams:
            kind, codec = stream.get("codec_type"), stream.get("codec_name")
            if kind not in ("video", "audio") or codec not in allowed_codecs:
                raise ProbeStop("unexpected_local_codec")
            safe = {"codec_type": kind, "codec_name": codec}
            if kind == "video":
                width, height = stream.get("width"), stream.get("height")
                if type(width) is not int or type(height) is not int or not 1 <= width <= 32768 or not 1 <= height <= 32768:
                    raise ProbeStop("invalid_local_dimensions")
                safe.update(width=width, height=height)
            safe_streams.append(safe)
        videos = [s for s in safe_streams if s["codec_type"] == "video"]
        expected_codec = {"png": "png", "jpg": "mjpeg", "jpeg": "mjpeg"}.get(extension)
        if len(videos) != 1 or expected_codec and (len(safe_streams) != 1 or videos[0]["codec_name"] != expected_codec):
            raise ProbeStop("original_format_mismatch")
        decoded = subprocess.run([str(ffmpeg), "-nostdin", "-nostats", *common, "-xerror", "-err_detect", "explode", "-i", str(path),
                    "-map", "0:v:0", "-map", "0:a?", "-fps_mode", "passthrough", "-progress", "pipe:1", "-f", "null", "-"], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", timeout=timeout(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if decoded.returncode != 0:
            raise ProbeStop("local_decode_failed")
        if len(decoded.stdout) > 128 * 1024:
            raise ProbeStop("local_decode_summary_limit")
        frames = re.findall(r"^frame=\s*([0-9]+)\s*$", decoded.stdout, re.MULTILINE)
        if not frames or not re.search(r"^progress=end\s*$", decoded.stdout, re.MULTILINE):
            raise ProbeStop("local_decode_summary_missing")
        decoded_frames = int(frames[-1])
        if decoded_frames < (2 if extension == "mp4" else 1):
            raise ProbeStop("insufficient_decoded_frames")
        evidence = {"decode": "PASS", "streams": safe_streams, "demuxer": demuxer,
                    "decoded_frames": decoded_frames,
                    "duration_seconds": None, "duration_evidence": "NOT_APPLICABLE_STILL_IMAGE"}
        if extension == "mp4":
            duration = float(data.get("format", {}).get("duration", "nan"))
            if not math.isfinite(duration) or duration <= 0:
                raise ProbeStop("missing_video_duration")
            evidence.update(duration_seconds=round(duration, 6), duration_evidence="FFPROBE")
        return evidence
    except ProbeStop:
        raise
    except subprocess.TimeoutExpired:
        raise ProbeStop("local_decode_or_probe_timeout") from None
    except Exception as exc:
        raise ProbeStop("local_validation_error", {"exception_type": type(exc).__name__}) from None
class Guard:
    def __init__(self, page, initial_bytes=0, *, clock=time.monotonic, sleep=time.sleep):
        self.sample = SimpleNamespace(page=page, assets=0, requests=2)
        self.formats=[]
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.deadline = self.started + WORKER_SECONDS
        self.last = None
        self.count = self.media_count = 0
        self.total_media = initial_bytes
        self.checkpoint=lambda value:None
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
                if kind=='media' and self.total_media>=TOTAL_LIMIT: self.stop('sample_byte_limit')
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
                        self.stop('redirect_rejected' if 300 <= response.status_code < 400 else 'http_'+str(response.status_code) if response.status_code in (401,403,412,429) else 'http_status_stop')
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
        signature = ({'png':b'\x89PNG\r\n\x1a\n','jpg':b'\xff\xd8\xff'}.get(self.formats[self.media_count-1]) if kind=='media' else None)
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
                        self.checkpoint(self.total_media)
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
                        validated, _, decoding = decode_metadata(bytes(raw_body), response.raw.headers)
                    except Stop:
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
def native_configuration(directory):
    from gallery_dl import config
    previous = copy.deepcopy(config._config)
    logging_level = logging.root.manager.disable
    config.clear()  # Embedded API does not load user's gallery-dl config files.
    for key, value in {'base-directory': str(directory), 'directory': [], 'filename': '{num:02}{suffix}.{extension}',
                       'retries': 0, 'timeout': 15, 'sleep-request': 0, 'sleep': 0,
                       'fallback': False, 'postprocessors': [], 'skip': True, 'input': False,
                       'write-pages': False, 'metadata-url': None, 'archive': None}.items():
        config.set(('extractor',), key, value)
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
