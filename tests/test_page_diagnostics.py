"""Diagnostic-only observations must preserve identity, request, and privacy gates."""
from email.message import Message
import gzip
import hashlib
import http.cookiejar
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import urllib.error

import pytest
# Import before replacing subprocess.Popen: yt-dlp defines a Popen subclass at import time.
import yt_dlp

SPEC = importlib.util.spec_from_file_location("diagnostic_page_probe", Path(__file__).parents[1] / "experiments/video_page_probe.py")
page = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(page)
PRIVATE = "PRIVATE_COOKIE_TOKEN_TITLE_SCRIPT_URL"


def html():
    return ('<html><head><title>' + PRIVATE + '</title></head><body><script>window.__INITIAL_STATE__='
            + json.dumps({"videoData": {"bvid": page.FIXED_BVID, "title": PRIVATE}})
            + ';</script></body></html>').encode()


def headers(*pairs):
    message = Message()
    for name, value in pairs:
        message.add_header(name, value)
    return message


def test_observes_structure_without_returning_content():
    result = page.response_diagnostics(html(), headers(("Content-Type", "text/html; charset=UTF-8")))
    assert result["content_type"] == "HTML" and result["charset"] == "UTF8"
    assert result["observed_bytes"] == len(html()) and len(result["observed_sha256"]) == 64
    assert result["utf8_strict_valid"] and result["initial_state_assignment"] and result["initial_state_token"]
    assert result["expected_bvid_literal"] and result["script_tag_count"] == 1
    assert all(result[key] for key in ("html_tag", "head_tag", "body_tag", "title_tag"))
    assert PRIVATE not in json.dumps(result) and "videoData" not in json.dumps(result)
    assert result["content_encoding"] == "MISSING" and result["bom"] == "NONE"


@pytest.mark.parametrize("value,category", [("identity", "IDENTITY"), (" GZip ", "GZIP"), ("deflate", "DEFLATE"),
    ("br", "BR"), ("zstd", "ZSTD"), ("gzip, br", "MULTIPLE_CODINGS"), (PRIVATE, "OTHER"), ("", "OTHER")])
def test_encoding_enums_never_return_unknown_values(value, category):
    result = page.response_diagnostics(b"", headers(("cOnTeNt-EnCoDiNg", value)))
    assert result["content_encoding"] == category and result["observed_bytes"] == 0
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("mime,category", [("application/json", "JSON"), ("text/plain", "TEXT"),
    ("application/xhtml+xml", "XHTML"), ("application/octet-stream", "BINARY"), (PRIVATE, "OTHER"), ("", "OTHER")])
def test_content_type_enums(mime, category):
    result = page.response_diagnostics(b"{}", {"CONTENT-TYPE": mime + "; charset=" + PRIVATE})
    assert result["content_type"] == category and result["charset"] == "OTHER"
    assert result["json_like_prefix"] and PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("charset,category", [('"UTF-8"', "UTF8"), ('gb18030', "GB18030"),
    ('utf-16le', "UTF16LE"), ('iso-8859-1', "LATIN1"), (PRIVATE, "OTHER"), ('"中文"', "OTHER")])
def test_charset_is_classified_not_used_to_decode(charset, category):
    body = html().decode().encode("utf-16")
    result = page.response_diagnostics(body, {"Content-Type": "text/html; charset=" + charset})
    assert result["charset"] == category and result["bom"] == "UTF16LE"
    assert not result["utf8_strict_valid"] and not result["initial_state_assignment"]
    with pytest.raises(page.ProbeStop, match="page_identity_missing"):
        page.page_identity(body)


@pytest.mark.parametrize("as_message", [False, True])
def test_mixed_case_duplicate_fields_are_ambiguous(as_message):
    pairs = [("Content-Type", "text/html; charset=utf-8"), ("CONTENT-type", PRIVATE),
             ("Content-Encoding", "identity"), ("CONTENT-encoding", PRIVATE)]
    values = headers(*pairs) if as_message else dict(pairs)
    result = page.response_diagnostics(html(), values)
    assert result["content_type_field_count"] == result["content_encoding_field_count"] == 2
    assert result["content_type"] == result["charset"] == result["content_encoding"] == "MULTIPLE_FIELDS"
    assert PRIVATE not in json.dumps(result)


def test_duplicate_charset_parameters_not_silently_selected():
    result = page.response_diagnostics(html(), {"Content-Type": "text/html; charset=utf-8; charset=" + PRIVATE})
    assert result["charset"] == "MULTIPLE_PARAMETERS" and PRIVATE not in json.dumps(result)


def test_gzip_is_observed_not_decompressed_or_promoted_to_pass():
    body = gzip.compress(html(), mtime=0)
    result = page.response_diagnostics(body, {"Content-Encoding": "gzip", "Content-Type": "text/html"})
    assert result["gzip_magic"] and not result["utf8_strict_valid"] and not result["initial_state_assignment"]
    assert result["analysis_layer"] == "RAW_DECHUNKED_NO_CONTENT_DECOMPRESSION"
    with pytest.raises(page.ProbeStop, match="page_identity_missing"):
        page.page_identity(body)
    assert page.page_identity(gzip.decompress(body))["bvid"] == page.FIXED_BVID  # Synthetic causal control only.


def test_utf8_assignment_and_invalid_text_remain_distinct_failure():
    body = html() + b"\xff"
    result = page.response_diagnostics(body, {})
    assert result["initial_state_assignment"] and not result["utf8_strict_valid"]
    with pytest.raises(page.ProbeStop, match="page_initial_state_invalid"):
        page.page_identity(body)


@pytest.mark.parametrize("body,marker", [(b"window.__INITIAL_DATA__={}", "initial_data_token"),
    (b"window.__playinfo__={}", "playinfo_assignment"), (b"window._riskdata_={}", "riskdata_marker"),
    (b"window.__riskdata__={}", "riskdata_marker"), (b"CAPTCHA", "verification_word_marker"),
    ("安全验证".encode(), "verification_word_marker"), (b"__INITIAL_STATE__", "initial_state_token")])
def test_markers_are_observations_not_identity_substitutes(body, marker):
    result = page.response_diagnostics(body, {})
    assert result[marker] and not result["initial_state_assignment"]
    with pytest.raises(page.ProbeStop, match="page_identity_missing"):
        page.page_identity(body)


def test_capped_prefix_not_analysed_as_full_page():
    result = page.response_diagnostics(b"x" * (page.download.MAX_METADATA_BYTES + 1), {})
    assert result["body_limit_exceeded"] and result["structure_analysis"] == "NOT_RUN_OVER_LIMIT"
    assert "initial_state_assignment" not in result and "utf8_strict_valid" not in result


@pytest.fixture
def offline_worker(monkeypatch):
    def forbidden(*a, **kw): pytest.fail("Offline test attempted external access")
    for target, name in ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"),
                         (socket.socket, "connect_ex"), (subprocess, "Popen")):
        monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(page.common, "load_bili_cookies", lambda *a: (http.cookiejar.CookieJar(), {}))
    state = {"responses": [], "calls": []}
    class Reply(io.BytesIO):
        status = 200
        def __init__(self, body, response_headers):
            super().__init__(body)
            self.headers = response_headers
    def open_request(request, timeout):
        assert timeout == 15
        state["calls"].append(request)
        item = state["responses"].pop(0)
        if isinstance(item, Exception): raise item
        return Reply(*item)
    for cls in (page.common.BoundedClient, page.PageTransport):
        original = cls.__init__
        def initialize(self, *a, _original=original, **kw):
            _original(self, *a, **kw)
            self.opener.open = open_request
        monkeypatch.setattr(cls, "__init__", initialize)
    state["responses"].append((b'{"code":0,"data":{"isLogin":true,"mid":1}}', {}))
    return state


@pytest.mark.parametrize("body,reason", [(html(), None), (b"window._riskdata_={}", "platform_verification_page"),
    (gzip.compress(html(), mtime=0), "page_identity_missing"),
    (b"x" * (page.download.MAX_METADATA_BYTES + 1), "metadata_size_limit")], ids=["valid", "risk", "gzip", "over-limit"])
def test_worker_captures_diagnostics_before_original_failure_gate(body, reason, offline_worker):
    offline_worker["responses"] += [urllib.error.HTTPError(page.FIXED_URL, 301, PRIVATE,
        headers(("Location", "/video/BV13x41117TL/")), io.BytesIO()), (body, headers(("Content-Type", "text/html"),
        ("Set-Cookie", PRIVATE), ("Server", PRIVATE)))]
    report = page.run_worker(PRIVATE, browser_confirmed=True, allow_canonical_redirect=True)
    assert report["request_count"] == len(offline_worker["calls"]) == 3, report
    assert report["page_redirect_count"] == 1 and report["media_downloads"] == 0
    assert report["response_diagnostics"]["observed_bytes"] == len(body)
    assert report["page"] == ("BLOCKED" if reason else "PASS")
    if reason: assert report["stop_reason"] == reason
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize("status", [412, 429])
def test_http_error_has_no_invented_body_diagnostics(status, offline_worker):
    offline_worker["responses"].append(urllib.error.HTTPError(page.FIXED_URL, status, PRIVATE, {}, io.BytesIO()))
    report = page.run_worker(PRIVATE, browser_confirmed=True, allow_canonical_redirect=True)
    assert report["request_count"] == len(offline_worker["calls"]) == 2
    assert report["stop_evidence"]["http_status"] == status and "response_diagnostics" not in report


def test_missing_browser_precondition_never_reads_credentials(monkeypatch):
    def forbidden(*a): pytest.fail("Prerequisite failed but credentials were accessed")
    monkeypatch.setattr(page.common, "load_bili_cookies", forbidden)
    report = page.run_worker(PRIVATE, browser_confirmed=False)
    assert report["request_count"] == 0 and "response_diagnostics" not in report


@pytest.mark.parametrize("content,reason", [(html(), None), (b"window._riskdata_={}", "platform_verification_page"),
    (b"window.__riskdata__={}", "platform_verification_page"), (b"CAPTCHA", "page_identity_missing"),
    (html().replace(page.FIXED_BVID.encode(), b"BV1bK411W797"), "page_identity_mismatch"),
    (html() + b"\xff", "page_initial_state_invalid")], ids=["valid", "risk", "risk2", "word", "wrong-bv", "bad-utf8"])
def test_gzip_worker_checks_decoded_content_and_preserves_raw_evidence(content, reason, offline_worker):
    raw = gzip.compress(content, mtime=0)
    offline_worker["responses"] += [urllib.error.HTTPError(page.FIXED_URL, 301, PRIVATE,
        headers(("Location", "/video/BV13x41117TL/")), io.BytesIO()),
        (raw, headers(("Content-Type", "text/html; charset=utf-8"), ("Content-Encoding", "gzip")))]
    report = page.run_worker(PRIVATE, browser_confirmed=True, allow_canonical_redirect=True)
    assert report["request_count"] == len(offline_worker["calls"]) == 3
    assert report["page_redirect_count"] == 1 and report["media_downloads"] == 0
    assert report["content_decoding"]["status"] == "DECODED"
    assert report["response_diagnostics"]["observed_sha256"] == hashlib.sha256(raw).hexdigest()
    decoded = report["decoded_response_diagnostics"]
    assert decoded["analysis_layer"] == "CONTENT_DECODED"
    assert decoded["observed_sha256"] == hashlib.sha256(content).hexdigest()
    assert decoded["observed_bytes"] == len(content)
    assert report["page"] == ("BLOCKED" if reason else "PASS")
    if reason:
        assert report["stop_reason"] == reason
    else:
        assert report["page_response_sha256"] == hashlib.sha256(raw).hexdigest()
        assert report["page_response_sha256"] != decoded["observed_sha256"]
        assert report["page_final_url"] == page.FIXED_URL + "/" and report["page_http_status"] == 200
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize("kind,reason", [("invalid", "metadata_gzip_invalid"), ("truncated", "metadata_gzip_incomplete"),
    ("trailing", "metadata_gzip_trailing_data"), ("oversized", "metadata_decoded_size_limit"),
    ("unknown", "content_encoding_unsupported")])
def test_failed_decoding_has_raw_evidence_but_no_decoded_summary(kind, reason, offline_worker):
    raw = gzip.compress(html(), mtime=0)
    if kind == "invalid": raw = PRIVATE.encode()
    if kind == "truncated": raw = raw[:-1]
    if kind == "trailing": raw += PRIVATE.encode()
    if kind == "oversized": raw = gzip.compress(b"x" * (page.download.MAX_METADATA_BYTES + 1), mtime=0)
    offline_worker["responses"].append((raw, {"Content-Encoding": PRIVATE if kind == "unknown" else "gzip"}))
    report = page.run_worker(PRIVATE, browser_confirmed=True)
    assert report["page"] == "BLOCKED" and report["stop_reason"] == reason
    assert report["request_count"] == len(offline_worker["calls"]) == 2
    assert report["content_decoding"]["status"] == "FAILED"
    assert report["response_diagnostics"]["observed_sha256"] == hashlib.sha256(raw).hexdigest()
    assert "decoded_response_diagnostics" not in report and "page_response_sha256" not in report
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize("content,reason,code", [(b'{"code":0,"data":{}}', None, None),
    (b'{"code":-101}', "api_stop", -101), (b'{"code":-352}', "api_stop", -352),
    (b"not json", "api_non_json", None), (b"window._riskdata_={}", "platform_verification_page", None)])
def test_shared_download_transport_checks_compressed_api(content, reason, code, offline_worker):
    transport = page.download.BoundedTransport(http.cookiejar.CookieJar())
    calls = []
    def reply(request, timeout):
        calls.append(request)
        result = io.BytesIO(gzip.compress(content, mtime=0))
        result.status, result.headers = 200, {"Content-Encoding": "gzip", "Content-Type": "application/json"}
        return result
    transport.opener.open = reply
    url = "https://api.bilibili.com/x/player/pagelist?bvid=" + page.FIXED_BVID
    if reason:
        with pytest.raises(page.ProbeStop, match=reason) as caught: transport.request(url)
        if code is not None: assert caught.value.evidence["api_code"] == code
    else:
        with transport.request(url) as response:
            assert response.read() == content and response.get_header("Content-Encoding") is None
    assert transport.count == len(calls) == 1


def test_real_extractor_receives_decoded_first_page_without_extra_request(monkeypatch, offline_worker, tmp_path):
    from yt_dlp.extractor.bilibili import BiliBiliIE
    transport = page.download.BoundedTransport(http.cookiejar.CookieJar())
    calls = []
    def reply(request, timeout):
        calls.append(request)
        result = io.BytesIO(gzip.compress(html(), mtime=0))
        result.status, result.headers = 200, {"Content-Encoding": "gzip", "Content-Type": "text/html; charset=utf-8"}
        return result
    transport.opener.open = reply
    def inspect(self, pattern, webpage, *a, **kw):
        assert webpage == html().decode()
        raise page.ProbeStop("offline_decoded_extractor_observed")
    monkeypatch.setattr(BiliBiliIE, "_search_json", inspect)
    with pytest.raises(page.ProbeStop, match="offline_decoded_extractor_observed"):
        page.download.download_one(page.download.SAMPLES[0], http.cookiejar.CookieJar(), transport,
                                   tmp_path, Path("ffmpeg.exe"), None)
    assert transport.count == len(calls) == 1 and not list(tmp_path.iterdir())


def test_media_branch_does_not_decompress(monkeypatch, offline_worker):
    transport = page.download.BoundedTransport(http.cookiejar.CookieJar())
    urls = ["https://test.bilivideo.com/video", "https://test.bilivideo.com/audio"]
    transport.allow_media(urls)
    raw = gzip.compress(PRIVATE.encode(), mtime=0)
    result = io.BytesIO(raw)
    result.status, result.headers = 206, {"Content-Encoding": "gzip", "Content-Type": "video/mp4"}
    transport.opener.open = lambda *a, **kw: result
    def forbidden(*a, **kw): pytest.fail("Media entered metadata decoding")
    monkeypatch.setattr(page.download, "decode_metadata", forbidden)
    with transport.request(urls[0]) as response:
        assert response.read() == raw and response.status == 206 and response.get_header("Content-Encoding") == "gzip"
    assert transport.count == 1
