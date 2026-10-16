import http.cookiejar
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import urllib.error

import pytest
from yt_dlp.networking import Request

SPEC = importlib.util.spec_from_file_location("video_page_probe", Path(__file__).parents[1] / "experiments" / "video_page_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def html(bvid=probe.FIXED_BVID, title="Synthetic video"):
    return ('<script>window.__INITIAL_STATE__=' + json.dumps({"videoData": {"bvid": bvid, "title": title}})
            + ';(function(){})();</script>').encode()


class Reply(io.BytesIO):
    status = 200
    headers = {"Content-Type": "text/html"}


@pytest.fixture(autouse=True)
def forbid_network_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline page test attempted network or credentials")
    for target, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                         (socket.socket, "connect"), (socket.socket, "connect_ex")):
        monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(probe.common, "load_bili_cookies", forbidden)


@pytest.fixture
def offline_run(monkeypatch):
    state = {"nav": json.dumps({"code": 0, "data": {"isLogin": True, "mid": 1}}).encode(),
             "page": html(), "calls": []}
    monkeypatch.setattr(probe.common, "load_bili_cookies", lambda *a: (http.cookiejar.CookieJar(), {}))
    account_init = probe.common.BoundedClient.__init__
    page_init = probe.PageTransport.__init__

    def opener(which):
        def open_request(request, timeout):
            assert timeout == 15
            state["calls"].append(request)
            response = state[which]
            if isinstance(response, BaseException):
                raise response
            return Reply(response)
        return open_request

    def initialize_account(self, jar, max_requests):
        assert max_requests == 1
        account_init(self, jar, max_requests)
        self.opener.open = opener("nav")

    def initialize_page(self, jar, initial_requests=0, **kwargs):
        assert initial_requests == 1
        page_init(self, jar, initial_requests, **kwargs)
        self.opener.open = opener("page")

    monkeypatch.setattr(probe.common.BoundedClient, "__init__", initialize_account)
    monkeypatch.setattr(probe.PageTransport, "__init__", initialize_page)
    return state


def test_worker_only_nav_and_fixed_page_with_initialized_headers(offline_run):
    result = probe.run_worker("SYNTHETIC", browser_confirmed=True)
    assert result["account"] == result["page"] == "PASS"
    assert result["request_count"] == 2 and result["media_downloads"] == 0
    assert [request.full_url for request in offline_run["calls"]] == [
        "https://api.bilibili.com/x/web-interface/nav", probe.FIXED_URL]
    headers = {k.lower(): v for k, v in offline_run["calls"][1].header_items()}
    assert {"user-agent", "accept", "accept-language", "sec-fetch-mode"} <= headers.keys()
    assert headers["accept-encoding"] == "identity"
    assert len(result["page_response_sha256"]) == 64
    assert result["page_identity"] == {"bvid": probe.FIXED_BVID, "title_present": True}
    assert "Synthetic video" not in json.dumps(result)


@pytest.mark.parametrize("nav", [b'{"code":-101}', b'{"code":0,"data":{"isLogin":false,"mid":1}}',
                                      urllib.error.HTTPError("SYNTHETIC", 412, "PRIVATE", {}, None)])
def test_failed_account_never_requests_page(nav, offline_run):
    offline_run["nav"] = nav
    result = probe.run_worker("SYNTHETIC", browser_confirmed=True)
    assert result["page"] == "BLOCKED" and result["request_count"] == len(offline_run["calls"]) == 1
    assert "page_identity" not in result


@pytest.mark.parametrize("status", [301, 302, 412, 429])
def test_page_http_failure_never_retries_or_follows(status, offline_run):
    offline_run["page"] = urllib.error.HTTPError(probe.FIXED_URL, status, "PRIVATE",
                                                 {"Location": probe.FIXED_URL + "/"}, io.BytesIO())
    result = probe.run_worker("SYNTHETIC", browser_confirmed=True)
    assert result["page"] == "BLOCKED" and result["stop_evidence"]["http_status"] == status
    assert result["request_count"] == len(offline_run["calls"]) == 2
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("body,reason", [(b"window._riskdata_={};", "platform_verification_page"),
    (b"window.__riskdata__={};", "platform_verification_page"), (b"CAPTCHA", "page_identity_missing"),
    (html("BV1bK411W797"), "page_identity_mismatch"), (html(title=" "), "page_title_missing"),
    (b"window.__INITIAL_STATE__=null;", "page_identity_mismatch"),
    (b"window.__INITIAL_STATE__=bad;", "page_initial_state_invalid")])
def test_page_requires_expected_identity_and_no_risk(body, reason, offline_run):
    offline_run["page"] = body
    result = probe.run_worker("SYNTHETIC", browser_confirmed=True)
    assert result["page"] == "BLOCKED" and result["stop_reason"] == reason
    assert result["request_count"] == 2 and result["media_downloads"] == 0


def test_page_size_cap_stops_before_identity(offline_run):
    offline_run["page"] = b"x" * (probe.download.MAX_METADATA_BYTES + 1)
    result = probe.run_worker("SYNTHETIC", browser_confirmed=True)
    assert result["stop_reason"] == "metadata_size_limit" and result["request_count"] == 2


def test_transport_caps_total_and_forbids_media_other_urls(monkeypatch):
    transport = probe.PageTransport(http.cookiejar.CookieJar(), initial_requests=1)
    calls = []
    def open_request(request, timeout):
        calls.append(request)
        return Reply(html())
    monkeypatch.setattr(transport.opener, "open", open_request)
    for url in ("https://api.bilibili.com/x/web-interface/nav", "https://upos.test.bilivideo.com/video.m4s"):
        with pytest.raises(probe.ProbeStop, match="fixed_page_only"):
            transport.request(Request(url))
    with pytest.raises(probe.ProbeStop, match="page_probe_forbids_media"):
        transport.allow_media([])
    transport.request(Request(probe.FIXED_URL)).close()
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        transport.request(Request(probe.FIXED_URL))
    assert len(calls) == 1 and transport.count == 2


def test_missing_browser_prerequisite_stops_before_credentials():
    result = probe.run_worker("PRIVATE", browser_confirmed=False)
    assert result["stop_reason"] == "browser_prerequisite_missing" and result["request_count"] == 0


def test_version_mismatch_stops_before_credentials(monkeypatch):
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda name: "wrong")
    result = probe.run_worker("PRIVATE", browser_confirmed=True)
    assert result["stop_reason"] == "dependency_version_mismatch" and result["request_count"] == 0


def test_main_guards_before_worker_and_reserves_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.common, "ROOT", tmp_path)
    output = tmp_path / ".artifacts" / "evidence.json"
    calls = []
    def run(profile, *, allow_canonical_redirect=False):
        assert allow_canonical_redirect is False
        calls.append(profile)
        assert json.loads(output.read_text())["page"] == "IN_PROGRESS"
        return {"page": "PASS", "request_count": 2}
    monkeypatch.setattr(probe, "run_bounded", run)
    for args in (["--profile", "PRIVATE", "--output", str(output)],
                 ["--profile", "PRIVATE", "--browser-confirmed", "--output", str(tmp_path / "outside.json")]):
        with pytest.raises(SystemExit):
            probe.main(args)
    assert not calls and not output.exists()
    args = ["--profile", "PRIVATE", "--browser-confirmed", "--output", str(output)]
    assert probe.main(args) == 0 and len(calls) == 1
    original = output.read_bytes()
    with pytest.raises(SystemExit):
        probe.main(args)
    assert output.read_bytes() == original and len(calls) == 1 and b"PRIVATE" not in original


def test_main_redacts_unexpected_error_and_does_not_invent_count(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.common, "ROOT", tmp_path)
    def fail(profile, *, allow_canonical_redirect=False):
        raise RuntimeError("PRIVATE_COOKIE_SIGNED_URL_PATH")
    monkeypatch.setattr(probe, "run_bounded", fail)
    output = tmp_path / ".artifacts" / "error.json"
    assert probe.main(["--profile", "PRIVATE", "--browser-confirmed", "--output", str(output)]) == 2
    report = json.loads(output.read_text())
    assert report["page"] == "BLOCKED" and report["request_count"] is None
    assert "PRIVATE" not in output.read_text() and report["http412_causation"] == "NOT_PROVEN"


@pytest.mark.parametrize("timeout", [False, True])
def test_worker_assignment_precedes_start_and_timeout_kills_job(monkeypatch, timeout):
    events = []
    class Job:
        def assign(self, process): events.append("assigned")
        def close(self): events.append("job_closed")
    class Child:
        returncode = 0
        def communicate(self, input=None, timeout=None):
            if input is not None:
                assert events == ["spawned", "assigned"]
                assert input == "START_U02_PAGE\n" and timeout == 60
                if state["timeout"]:
                    raise subprocess.TimeoutExpired("synthetic", timeout)
                return '{"page":"PASS","request_count":2}', None
            assert events[-1] == "job_closed" and timeout == 5
            return "", None
        def poll(self): return 0
    state = {"timeout": timeout}
    def spawn(command, **kwargs):
        assert "--browser-confirmed" in command and "--worker" in command
        assert kwargs["stdin"] == subprocess.PIPE
        events.append("spawned")
        return Child()
    monkeypatch.setattr(probe.download, "WindowsJob", Job)
    monkeypatch.setattr(probe.subprocess, "Popen", spawn)
    result = probe.run_bounded("SYNTHETIC")
    assert events[-1] == "job_closed"
    assert result["request_count"] is None if timeout else result["request_count"] == 2
