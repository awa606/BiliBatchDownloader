"""Diagnostic-only observations must preserve identity, request, and privacy gates."""
from email.message import Message
import gzip
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
