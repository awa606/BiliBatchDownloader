"""Synthetic redirect responses only: no network, profile, or media access."""
from email.message import Message
import http.cookiejar
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import urllib.error

import pytest
from yt_dlp.networking import Request

SPEC = importlib.util.spec_from_file_location("canonical_page_probe", Path(__file__).parents[1] / "experiments/video_page_probe.py")
page = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(page)
probe = page.download
URL = page.FIXED_URL


def canonical(url):
    path, sep, query = url.partition("?")
    return path + "/" + sep + query


def redirect(*locations, status=301):
    headers = Message()
    for value in locations:
        headers.add_header("Location", value)
    return urllib.error.HTTPError("PRIVATE", status, "PRIVATE", headers, io.BytesIO())


def html(bvid=page.FIXED_BVID):
    return ('window.__INITIAL_STATE__=' + json.dumps({"videoData": {"bvid": bvid, "title": "Synthetic"}})).encode()


class Reply(io.BytesIO):
    status = 200
    headers = {"Content-Type": "text/html"}


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network, credential or subprocess access")
    for target, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                         (socket.socket, "connect"), (socket.socket, "connect_ex"),
                         (probe.common, "load_bili_cookies"), (subprocess, "Popen")):
        monkeypatch.setattr(target, name, forbidden)


def transport(monkeypatch, responses, *, enabled=True, limit=36, initial=1):
    result = probe.BoundedTransport(http.cookiejar.CookieJar(), initial,
        allow_canonical_redirect=enabled, request_limit=limit)
    calls = []
    def open_request(request, timeout):
        assert timeout == 15
        calls.append(request)
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return Reply(item)
    monkeypatch.setattr(result.opener, "open", open_request)
    return result, calls


@pytest.mark.parametrize("sample", probe.SAMPLES)
@pytest.mark.parametrize("relative", [False, True])
def test_exact_observed_target_and_real_final_url(sample, relative, monkeypatch):
    original = probe.sample_url(*sample)
    target = canonical(original)
    location = target.removeprefix("https://www.bilibili.com") if relative else target
    tx, calls = transport(monkeypatch, [redirect(location), html()])
    response = tx.request(Request(original, headers={"Cookie": "PRIVATE", "Authorization": "PRIVATE", "Range": "bytes=0-7"}),
        default_headers={"User-Agent": "Synthetic", "Referer": "https://www.bilibili.com/"})
    assert response.url == target
    response.close()
    assert [r.full_url for r in calls] == [original, target]
    assert tx.count == 3 and tx.page_redirect_count == 1 and tx.redirect_count == 0
    for request in calls:
        headers = {k.lower(): v for k, v in request.header_items()}
        assert headers["range"] == "bytes=0-7" and headers["user-agent"] == "Synthetic"
        assert headers["referer"] == "https://www.bilibili.com/" and headers["accept-encoding"] == "identity"
        assert not {"cookie", "authorization", "proxy-authorization"} & headers.keys()
    event = tx.page_redirect_events[0]
    assert event["decision"] == "ALLOW" and all(event[k] for k in ("same_origin", "same_bvid", "same_query", "trailing_slash_only"))
    # A successful observed redirect never grants future direct slash requests.
    with pytest.raises(probe.ProbeStop, match="request_origin_or_endpoint_not_allowed"):
        tx.request(target)
    assert len(calls) == 2


@pytest.mark.parametrize("locations,status", [
    ([], 301), ([canonical(URL), canonical(URL)], 301), ([canonical(URL)], 302),
    ([canonical(URL)], 307), ([canonical(URL)], 308), ([""], 301),
    (["https://PRIVATE.invalid/video/BV13x41117TL/"], 301),
    (["http://www.bilibili.com/video/BV13x41117TL/"], 301),
    (["https://www.bilibili.com:443/video/BV13x41117TL/"], 301),
    (["https://PRIVATE@www.bilibili.com/video/BV13x41117TL/"], 301),
    (["//www.bilibili.com/video/BV13x41117TL/"], 301),
    (["/video/BV1bK411W797/"], 301), ([canonical(URL) + "?PRIVATE=1"], 301),
    ([canonical(URL) + "?"], 301), ([canonical(URL) + "#PRIVATE"], 301),
    ([canonical(URL) + "#"], 301), (["/video/%42V13x41117TL/"], 301),
    (["/video/BV13x41117TL//"], 301), (["/login"], 301),
    (["/video/BV13x41117TL/ PRIVATE"], 301), (["/video/BV13x41117TL/\t"], 301),
    (["/video/BV13x41117TL/\r\nPRIVATE"], 301), (["/video/BV13x41117TL/\\"], 301),
    (["https://[PRIVATE/video/x"], 301), (["/video/BV13x41117TL/中文"], 301),
    ([URL], 301),
])
def test_rejected_target_stops_before_follow_and_redacts(locations, status, monkeypatch):
    tx, calls = transport(monkeypatch, [redirect(*locations, status=status)])
    with pytest.raises(probe.ProbeStop) as error:
        tx.request(URL)
    assert tx.count == 2 and len(calls) == 1 and tx.page_redirect_count == 0
    evidence = json.dumps({"error": error.value.evidence, "events": tx.page_redirect_events})
    assert "PRIVATE" not in evidence and "www.bilibili.com" not in evidence
    assert tx.page_redirect_events[0]["decision"] == "DENY"


@pytest.mark.parametrize("query", ["", "?p=2", "?p=1&p=1", "?p=1&PRIVATE=1", "?p=01", "?%70=1"])
def test_multipart_query_must_remain_byte_exact(query, monkeypatch):
    original = probe.sample_url("BV1bK411W797", 1)
    tx, calls = transport(monkeypatch, [redirect("/video/BV1bK411W797/" + query)])
    with pytest.raises(probe.ProbeStop):
        tx.request(original)
    assert len(calls) == 1 and tx.count == 2


def test_default_off_and_budget_checked_on_physical_hop(monkeypatch):
    tx, calls = transport(monkeypatch, [redirect(canonical(URL))], enabled=False)
    with pytest.raises(probe.ProbeStop):
        tx.request(URL)
    assert len(calls) == 1 and tx.count == 2
    assert tx.page_redirect_events[0]["reason"] == "canonical_redirect_disabled"
    tx, calls = transport(monkeypatch, [redirect(canonical(URL))], limit=2)
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        tx.request(URL)
    assert len(calls) == 1 and tx.count == 2 and tx.page_redirect_count == 0
    tx, calls = transport(monkeypatch, [redirect(canonical(URL)), html()], limit=3)
    tx.request(URL).close()
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        tx.request(URL)
    assert len(calls) == 2 and tx.count == 3


@pytest.mark.parametrize("target", [URL, canonical(URL), canonical(URL) + "/"])
def test_second_redirect_and_loop_never_follow(target, monkeypatch):
    tx, calls = transport(monkeypatch, [redirect(canonical(URL)), redirect(target)])
    with pytest.raises(probe.ProbeStop):
        tx.request(URL)
    assert len(calls) == 2 and tx.count == 3 and tx.page_redirect_count == 1
    assert tx.page_redirect_events[-1]["reason"] == "page_redirect_already_followed"


def test_each_fixed_page_only_once_and_api_never_followed(monkeypatch):
    tx, calls = transport(monkeypatch, [redirect(canonical(URL)), html(), redirect(canonical(URL))])
    tx.request(URL).close()
    with pytest.raises(probe.ProbeStop):
        tx.request(URL)
    assert len(calls) == 3 and tx.page_redirect_count == 1
    api = "https://api.bilibili.com/x/player/pagelist"
    tx, calls = transport(monkeypatch, [redirect(canonical(api))])
    with pytest.raises(probe.ProbeStop):
        tx.request(api)
    assert len(calls) == 1 and tx.page_redirect_events[0]["reason"] == "not_fixed_video_page"


@pytest.mark.parametrize("outcome,expected", [
    (html(), "PASS"), (redirect(status=412), "http_or_redirect_stop"),
    (redirect(status=429), "http_or_redirect_stop"), (b"window._riskdata_={}", "platform_verification_page"),
    (html("BV1bK411W797"), "page_identity_mismatch"), (TimeoutError("PRIVATE"), "transport_stop")])
def test_real_worker_three_request_flow_and_terminal_failures(outcome, expected, monkeypatch):
    calls = []
    monkeypatch.setattr(probe.common, "load_bili_cookies", lambda *a: (http.cookiejar.CookieJar(), {}))
    responses = [json.dumps({"code": 0, "data": {"isLogin": True, "mid": 1}}).encode(), redirect(canonical(URL)), outcome]
    account_init, tx_init = probe.common.BoundedClient.__init__, page.PageTransport.__init__
    def open_request(request, timeout):
        calls.append(request)
        item = responses.pop(0)
        if isinstance(item, BaseException): raise item
        return Reply(item)
    def account(self, *a, **kw):
        account_init(self, *a, **kw)
        self.opener.open = open_request
    def tx(self, *a, **kw):
        tx_init(self, *a, **kw)
        assert self.request_limit == 3
        self.opener.open = open_request
    monkeypatch.setattr(probe.common.BoundedClient, "__init__", account)
    monkeypatch.setattr(page.PageTransport, "__init__", tx)
    report = page.run_worker("PRIVATE", browser_confirmed=True, allow_canonical_redirect=True)
    assert len(calls) == report["request_count"] == 3 and report["media_downloads"] == 0
    assert report["page_redirect_count"] == 1 and report["media_redirect_count"] == 0
    assert "PRIVATE" not in json.dumps(report)
    if expected == "PASS":
        assert report["page"] == "PASS" and report["page_final_url"] == canonical(URL)
    else:
        assert report["page"] == "BLOCKED" and report["stop_reason"] == expected


@pytest.mark.parametrize("module", [page, probe])
@pytest.mark.parametrize("enabled", [False, True])
def test_parent_forwards_opt_in_to_worker_and_keeps_deadline(module, enabled, monkeypatch):
    class Job:
        def assign(self, child): pass
        def close(self): pass
    class Child:
        returncode = 0
        def communicate(self, input=None, timeout=None):
            assert timeout == module.WORKER_TIMEOUT
            return '{}', None
        def poll(self): return 0
    def spawn(command, **kw):
        assert ("--allow-canonical-redirect" in command) is enabled
        return Child()
    monkeypatch.setattr(probe, "WindowsJob", Job)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    args = ["PRIVATE"] if module is page else ["PRIVATE", Path("synthetic"), Path("ffmpeg.exe")]
    module.run_bounded(*args, allow_canonical_redirect=enabled)


@pytest.mark.parametrize("module", [page, probe])
def test_worker_cli_forwards_opt_in(module, monkeypatch):
    def run(*a, **kw):
        assert kw["allow_canonical_redirect"] is True
        return {}
    monkeypatch.setattr(module, "run_worker", run)
    monkeypatch.setattr(sys, "stdin", io.StringIO("START_U02_PAGE\n" if module is page else "START_U02\n"))
    args = ["--worker", "--profile", "PRIVATE", "--allow-canonical-redirect"]
    args += ["--browser-confirmed"] if module is page else ["--workdir", "synthetic", "--ffmpeg", "ffmpeg.exe"]
    assert module.main(args) == 0


@pytest.mark.parametrize("module", [page, probe])
def test_public_cli_records_policy_and_passes_it_to_parent(module, tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe.common, "ROOT", tmp_path)
    (tmp_path / ".artifacts").mkdir()
    def run(*a, **kw):
        assert kw["allow_canonical_redirect"] is True
        return {"page": "PASS", "download": "PASS", "assets": [{}, {}, {}]}
    monkeypatch.setattr(module, "run_bounded", run)
    args = ["--profile", "PRIVATE", "--allow-canonical-redirect"]
    if module is page:
        output = tmp_path / ".artifacts" / "report.json"
        args += ["--browser-confirmed", "--output", str(output)]
    else:
        ffmpeg = tmp_path / "ffmpeg.exe"
        ffmpeg.write_bytes(b"SYNTHETIC")
        workdir = tmp_path / ".artifacts" / "video-download-synthetic"
        output = workdir / "report.json"
        args += ["--workdir", str(workdir), "--ffmpeg", str(ffmpeg)]
    assert module.main(args) == 0
    result = json.loads(output.read_text())
    assert result["allow_canonical_redirect"] is True
    assert result["request_limit"] == (3 if module is page else 36)
    assert "PRIVATE" not in output.read_text()


def test_download_36_request_cap_counts_redirect_and_keeps_cdn_separate(monkeypatch):
    tx, calls = transport(monkeypatch, [redirect(canonical(URL))], initial=35)
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        tx.request(URL)
    assert tx.count == 36 and len(calls) == 1 and tx.page_redirect_count == 0
    source, target = "https://test.bilivideo.com/video", "https://test.bilivideo.cn/video"
    tx, calls = transport(monkeypatch, [redirect(target, status=302)])
    tx.allow_media([source, "https://test.bilivideo.com/audio"])
    with pytest.raises(probe.ProbeStop, match="transport_stop"):
        tx.request(source)  # Exhaust the fake responses after the allowed CDN hop.
    assert tx.redirect_count == 1 and tx.page_redirect_count == 0 and tx.page_redirect_events == []
    assert len(calls) == 2 and tx.count == 3


@pytest.mark.parametrize("duplicate", [False, True])
def test_location_field_name_is_case_insensitive_and_duplicates_are_rejected(duplicate):
    headers = Message()
    headers.add_header("lOcAtIoN", canonical(URL))
    if duplicate:
        headers.add_header("LOCATION", canonical(URL))
    target, evidence = probe.canonical_redirect(URL, 301, headers, enabled=True, already_followed=False)
    assert target == (None if duplicate else canonical(URL))
    assert evidence["location_count"] == (2 if duplicate else 1)
