import ctypes
from ctypes import wintypes
import hashlib
import http.cookiejar
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import urllib.error

import pytest
from yt_dlp.networking import Request

SPEC = importlib.util.spec_from_file_location("video_download_probe", Path(__file__).parents[1] / "experiments" / "video_download_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
PAGE = probe.sample_url(*probe.SAMPLES[0])
VIDEO = "https://upos.test.bilivideo.com/video.m4s?token=SYNTHETIC"
AUDIO = "https://upos.test.bilivideo.com/audio.m4s?token=SYNTHETIC"


def test_exclusive_workdir_and_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    (tmp_path / ".artifacts").mkdir()
    target = tmp_path / ".artifacts" / "video-download-one"
    assert probe.check_workdir(target, create=True) == target
    with pytest.raises(FileExistsError):
        probe.check_workdir(target, create=True)
    for invalid in (tmp_path / "video-download-outside", tmp_path / ".artifacts" / "wrong", target / "video-download-nested"):
        with pytest.raises(probe.ProbeStop):
            probe.check_workdir(invalid, create=True)


def test_tool_validation_requires_explicit_ffmpeg_and_reports_optional_probe(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    with pytest.raises(probe.ProbeStop):
        probe.check_ffmpeg(ffmpeg)
    ffmpeg.write_bytes(b"SYNTHETIC")
    assert probe.check_ffmpeg(ffmpeg)[1] is None
    (tmp_path / "ffprobe.exe").write_bytes(b"SYNTHETIC")
    assert probe.check_ffmpeg(ffmpeg)[0] == ffmpeg


@pytest.mark.parametrize("url", ["http://test.bilivideo.com/x", "https://bilivideo.com.evil.test/x",
                                    "https://user:password@test.bilivideo.com/x", "https://test.bilivideo.com:444/x",
                                    "https://127.0.0.1/x", "file:///secret"])
def test_reject_nonapproved_media_origin(url):
    assert not probe.cdn_url(url)


class Reply(io.BytesIO):
    def __init__(self, body=b"{}", mime="application/json", status=200):
        super().__init__(body)
        self.headers, self.status = {"Content-Type": mime}, status


def transport_with(monkeypatch, responses, initial=0):
    transport = probe.BoundedTransport(http.cookiejar.CookieJar(), initial)
    calls = []
    def open_request(request, timeout):
        calls.append(request)
        assert timeout == 15
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(transport.opener, "open", open_request)
    return transport, calls


def redirect(location):
    return urllib.error.HTTPError("https://synthetic.test", 302, "secret omitted", {"Location": location}, io.BytesIO())


def capture_real_first_webpage(monkeypatch, tmp_path, *, defaults=None, per_request=None):
    """Exercise download_one -> real BiliBiliIE -> ResearchYDL -> transport.

    Stop at the urllib boundary, before any response, network, credential load,
    media or subprocess. Only synthetic empty cookies enter download_one.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.bilibili import BiliBiliIE
    from yt_dlp.networking._urllib import UrllibRH

    def forbidden(*args, **kwargs):
        pytest.fail("Offline header regression attempted network, credentials or subprocess")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(probe.common, "load_bili_cookies", forbidden)
    monkeypatch.setattr(probe.subprocess, "run", forbidden)
    monkeypatch.setattr(probe.subprocess, "Popen", forbidden)
    initialized = []
    upstream_init = YoutubeDL.__init__

    def initialize(downloader, *args, **kwargs):
        upstream_init(downloader, *args, **kwargs)
        # Inject after native initialization so synthetic Cookie is tested as a
        # raw default header, rather than consumed by upstream cookie loading.
        downloader.params["http_headers"].update(defaults or {})
        initialized.append(dict(downloader.params["http_headers"]))

    monkeypatch.setattr(YoutubeDL, "__init__", initialize)
    if per_request is not None:
        monkeypatch.setattr(BiliBiliIE, "geo_verification_headers", lambda self: per_request.copy())
    jar = http.cookiejar.CookieJar()
    transport = probe.BoundedTransport(jar)
    calls = []

    def capture(request, timeout):
        assert timeout == 15
        calls.append(request)
        raise probe.ProbeStop("offline_first_request_captured")

    monkeypatch.setattr(transport.opener, "open", capture)
    with pytest.raises(probe.ProbeStop, match="offline_first_request_captured"):
        probe.download_one(probe.SAMPLES[0], jar, transport, tmp_path, Path("ffmpeg.exe"), None)
    assert len(initialized) == len(calls) == transport.count == 1
    assert calls[0].full_url == PAGE and calls[0].method == "GET"
    assert transport.redirect_count == 0 and not transport.media_urls
    assert not list(tmp_path.iterdir())
    # Compare against the pinned upstream request handler's actual merge rule.
    with UrllibRH(logger=probe.video.StopLogger(), headers=initialized[0]) as handler:
        expected = {key.lower(): value for key, value in handler._merge_headers(per_request or {}).items()}
    return {key.lower(): value for key, value in calls[0].header_items()}, expected


def test_real_first_webpage_preserves_initialized_upstream_default_headers(monkeypatch, tmp_path):
    actual, expected = capture_real_first_webpage(monkeypatch, tmp_path)
    for name in ("user-agent", "accept", "accept-language", "sec-fetch-mode"):
        assert actual.get(name) == expected[name]
    assert actual["accept-encoding"] == "identity"


def test_real_first_webpage_request_overrides_defaults_case_insensitively(monkeypatch, tmp_path):
    actual, expected = capture_real_first_webpage(monkeypatch, tmp_path,
        defaults={"User-Agent": "SYNTHETIC_DEFAULT", "X-Probe": "default", "Range": "bytes=0-7",
                  "Referer": "https://www.bilibili.com/"},
        per_request={"uSeR-aGeNt": "SYNTHETIC_REQUEST", "x-pRoBe": "request", "rAnGe": "bytes=8-15"})
    for name in ("user-agent", "x-probe", "range", "referer", "accept"):
        assert actual.get(name) == expected[name]
    assert actual["user-agent"] == "SYNTHETIC_REQUEST" and actual["range"] == "bytes=8-15"


@pytest.mark.parametrize("header_layer", ["defaults", "per_request", "both"])
def test_real_first_webpage_filters_sensitive_headers_after_merge(header_layer, monkeypatch, tmp_path):
    sensitive = {"cOoKiE": "SYNTHETIC_PRIVATE", "aUtHoRiZaTiOn": "SYNTHETIC_PRIVATE",
                 "pRoXy-AuThOrIzAtIoN": "SYNTHETIC_PRIVATE", "aCcEpT-EnCoDiNg": "gzip"}
    actual, expected = capture_real_first_webpage(monkeypatch, tmp_path,
        defaults=sensitive if header_layer in {"defaults", "both"} else {},
        per_request={**(sensitive if header_layer in {"per_request", "both"} else {}),
                     "Referer": "https://www.bilibili.com/", "Range": "bytes=8-15"})
    assert not {"cookie", "authorization", "proxy-authorization"}.intersection(actual)
    assert "SYNTHETIC_PRIVATE" not in actual.values()
    assert actual["accept-encoding"] == "identity"
    assert actual["referer"] == expected["referer"] and actual["range"] == expected["range"]


def test_header_merge_does_not_mutate_inputs_or_emit_case_duplicates(monkeypatch):
    defaults = {"User-Agent": "SYNTHETIC_DEFAULT", "Referer": "https://www.bilibili.com/",
                "Cookie": "SYNTHETIC_PRIVATE"}
    request = Request(PAGE, headers={"uSeR-aGeNt": "SYNTHETIC_REQUEST", "Range": "bytes=4-9"})
    before_defaults, before_request = defaults.copy(), dict(request.headers)
    transport, calls = transport_with(monkeypatch, [Reply(b"<html></html>", "text/html")])
    transport.request(request, default_headers=defaults).close()
    assert defaults == before_defaults and dict(request.headers) == before_request
    names = [name.lower() for name, value in calls[0].header_items()]
    assert len(names) == len(set(names)) and names.count("user-agent") == 1


def test_metadata_redirect_is_not_followed(monkeypatch):
    transport, calls = transport_with(monkeypatch, [redirect(PAGE)])
    with pytest.raises(probe.ProbeStop, match="http_or_redirect_stop"):
        transport.request(Request(PAGE))
    assert len(calls) == 1


def test_media_redirect_is_bounded_and_counted_without_copied_secrets(monkeypatch):
    transport, calls = transport_with(monkeypatch, [redirect("https://other.bilivideo.cn/m4s"), Reply(b"video", "video/mp4")])
    transport.allow_media([VIDEO, AUDIO])
    result = transport.request(Request(VIDEO, headers={"Cookie": "PRIVATE", "Authorization": "PRIVATE"}))
    assert result.read() == b"video"
    assert transport.count == 2 and transport.redirect_count == 1
    assert all(not req.has_header("Cookie") and not req.has_header("Authorization") for req in calls)


def test_media_third_redirect_stops(monkeypatch):
    transport, calls = transport_with(monkeypatch, [redirect("https://a.bilivideo.com/x") for _ in range(3)])
    transport.allow_media([VIDEO, AUDIO])
    with pytest.raises(probe.ProbeStop, match="media_redirect_not_allowed"):
        transport.request(Request(VIDEO))
    assert len(calls) == 3


def test_media_redirect_to_external_host_stops_before_request(monkeypatch):
    transport, calls = transport_with(monkeypatch, [redirect("https://evil.test/collect")])
    transport.allow_media([VIDEO, AUDIO])
    with pytest.raises(probe.ProbeStop, match="media_redirect_not_allowed"):
        transport.request(Request(VIDEO))
    assert len(calls) == 1


def test_request_budget_includes_account_and_redirects(monkeypatch):
    transport, calls = transport_with(monkeypatch, [Reply(b"<html></html>", "text/html")], initial=35)
    transport.request(Request(PAGE))
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        transport.request(Request(PAGE))
    assert len(calls) == 1 and transport.count == 36


@pytest.mark.parametrize("body,reason", [(b"window._riskdata_={};", "platform_verification_page"),
                                         (b'{"code":-101}', "api_stop"), (b"CAPTCHA", "api_non_json")])
def test_platform_or_login_signal_stops(body, reason, monkeypatch):
    transport, calls = transport_with(monkeypatch, [Reply(body)])
    with pytest.raises(probe.ProbeStop, match=reason):
        transport.request(Request("https://api.bilibili.com/x/web-interface/nav"))
    assert len(calls) == 1


def test_arbitrary_request_never_reaches_network(monkeypatch):
    transport, calls = transport_with(monkeypatch, [])
    with pytest.raises(probe.ProbeStop):
        transport.request(Request("https://api.bilibili.com/x/forbidden"))
    with pytest.raises(probe.ProbeStop):
        transport.request(Request(PAGE, method="POST", data=b"secret"))
    assert not calls


def test_large_stream_is_stopped(monkeypatch):
    reader = probe.LimitedReader(Reply(b"123456", "video/mp4"), 5)
    with pytest.raises(probe.ProbeStop, match="media_size_limit"):
        reader.read()
    assert reader.closed


def formats():
    return [{"format_id": "16", "height": 360, "vcodec": "avc1", "acodec": "none", "ext": "mp4", "url": VIDEO, "protocol": "https"},
            {"format_id": "80", "height": 1080, "vcodec": "avc1", "acodec": "none", "ext": "mp4", "url": VIDEO, "protocol": "https"},
            {"format_id": "30216", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "url": AUDIO, "protocol": "https"}]


def test_formats_force_360p_and_separate_tracks():
    selected = probe.choose_streams(formats())
    assert selected[0]["height"] == 360
    assert selected[1]["vcodec"] == "none"
    with pytest.raises(probe.ProbeStop):
        probe.choose_streams(formats()[1:])
    with pytest.raises(probe.ProbeStop):
        probe.choose_streams([{**fmt, "fragments": [{"url": "private"}]} for fmt in formats()])


def test_real_upstream_raw_dash_records_without_protocol():
    from yt_dlp.extractor.bilibili import BilibiliBaseIE
    # Execute the pinned upstream pure parser, not our own hand-built output.
    # Input is a synthetic DASH payload; no API, browser, cookie or media access.
    upstream_formats = BilibiliBaseIE().extract_formats({
        "support_formats": [{"quality": 16, "new_description": "360P"}],
        "dash": {
            "video": [{"id": 16, "baseUrl": VIDEO, "mimeType": "video/mp4", "codecs": "avc1.64001E",
                       "width": 640, "height": 360, "bandwidth": 360000}],
            "audio": [{"id": 30216, "baseUrl": AUDIO, "mimeType": "audio/mp4", "codecs": "mp4a.40.2", "bandwidth": 64000}],
        },
    })
    assert all("protocol" not in fmt for fmt in upstream_formats)
    selected_video, selected_audio = probe.choose_streams(upstream_formats)
    assert selected_video["height"] == 360
    assert selected_audio["acodec"] == "mp4a.40.2"
    assert selected_video["url"] == VIDEO and selected_audio["url"] == AUDIO


@pytest.mark.parametrize("protocol", ["m3u8_native", "http_dash_segments", "rtmp", "", False])
def test_explicit_non_http_protocol_is_never_inferred(protocol):
    with pytest.raises(probe.ProbeStop):
        probe.choose_streams([{**fmt, "protocol": protocol} for fmt in formats()])


def fake_probe_result(video=True, audio=True, duration="10", height=360, video_codec="h264", audio_codec="aac"):
    streams = ([{"codec_type": "video", "codec_name": video_codec, "height": height}] if video else []) + ([{"codec_type": "audio", "codec_name": audio_codec}] if audio else [])
    return subprocess.CompletedProcess([], 0, json.dumps({"streams": streams, "format": {"duration": duration}}).encode())


def fake_decode_result():
    return subprocess.CompletedProcess([], 0, b"out_time_us=10000000\nprogress=end\n",
        b"Input #0 PRIVATE_PATH\n  Stream #0:0[0x1](und): Video: h264, yuv420p, 640x360\n  Stream #0:1(und): Audio: aac\nOutput #0")


def test_actual_file_digest_and_tracks_are_checked(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"SYNTHETIC_NOT_REAL_MEDIA")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command[command.index("-f") + 1] == "mov"
        return fake_probe_result() if command[0] == "ffprobe.exe" else fake_decode_result()
    monkeypatch.setattr(probe.subprocess, "run", run)
    result = probe.inspect_file(path, Path("ffprobe.exe"), 10, ffmpeg=Path("ffmpeg.exe"))
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["video_tracks"] == result["audio_tracks"] == 1
    assert result["preview"] == result["playback"] == "NOT_TESTED"
    assert len(calls) == 2
    assert result["verification_method"] == "ffprobe_and_full_decode"
    assert result["full_decode"] == "PASS" and result["video_codec"] == "h264" and result["audio_codec"] == "aac"


@pytest.mark.parametrize("response", [fake_probe_result(audio=False), fake_probe_result(video=False),
                                       fake_probe_result(duration="1"), fake_probe_result(duration="NaN"), fake_probe_result(height=720),
                                       fake_probe_result(video_codec="hevc"), fake_probe_result(audio_codec="opus")])
def test_bad_media_does_not_pass(response, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"SYNTHETIC")
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: response)
    with pytest.raises(probe.ProbeStop, match="media_track_or_duration"):
        probe.inspect_file(path, Path("ffprobe.exe"), 10, ffmpeg=Path("ffmpeg.exe"))


def test_ffmpeg_only_full_decode_is_explicit_and_messages_never_persist(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"SYNTHETIC")
    def run(command, **kwargs):
        assert "-xerror" in command and "-progress" in command
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command[command.index("-f") + 1] == "mov"
        assert kwargs["timeout"] == 45
        return subprocess.CompletedProcess(command, 0, b"out_time_us=10000000\nprogress=end\n",
            b"Input #0 PRIVATE_PATH\n  Stream #0:0[0x1](und): Video: h264, yuv420p, 640x360\n  Stream #0:1(und): Audio: aac\nOutput #0")
    monkeypatch.setattr(probe.subprocess, "run", run)
    result = probe.inspect_file(path, None, 10, ffmpeg=Path("ffmpeg.exe"))
    assert result["verification_method"] == "ffmpeg_full_decode"
    assert result["ffprobe"] == "NOT_AVAILABLE"
    assert result["audio_tracks"] == result["video_tracks"] == 1
    assert "PRIVATE_PATH" not in json.dumps(result)


def test_ffmpeg_missing_end_marker_cannot_pass(monkeypatch):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0,
        b"out_time_us=10000000\nprogress=continue\n", b"Stream #0:0: Video: h264, 640x360\nStream #0:1: Audio: aac"))
    with pytest.raises(probe.ProbeStop, match="ffmpeg_decode_summary_missing"):
        probe.decode_with_ffmpeg(Path("synthetic.mp4"), Path("ffmpeg.exe"))


def test_ffprobe_headers_pass_but_corrupt_frames_do_not(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"SYNTHETIC")
    def run(command, **kwargs):
        return fake_probe_result() if command[0] == "ffprobe.exe" else subprocess.CompletedProcess(command, 1, b"", b"PRIVATE_ERROR")
    monkeypatch.setattr(probe.subprocess, "run", run)
    with pytest.raises(probe.ProbeStop, match="ffmpeg_full_decode_failed"):
        probe.inspect_file(path, Path("ffprobe.exe"), 10, ffmpeg=Path("ffmpeg.exe"))


@pytest.mark.parametrize("codec_text", [b"Video: hevc, 640x360\nStream #0:1: Audio: aac",
                                        b"Video: h264, 640x360\nStream #0:1: Audio: opus"])
def test_ffmpeg_only_rejects_actual_wrong_codecs(codec_text, monkeypatch):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0,
        b"out_time_us=10000000\nprogress=end\n", b"Stream #0:0: " + codec_text))
    with pytest.raises(probe.ProbeStop, match="unexpected_decoded_codec"):
        probe.decode_with_ffmpeg(Path("synthetic.mp4"), Path("ffmpeg.exe"))


def test_real_upstream_merger_command_restricts_every_input(tmp_path, monkeypatch):
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor import ffmpeg as upstream
    visual, audio = tmp_path / "video.mp4", tmp_path / "audio.m4a"
    visual.write_bytes(b"SYNTHETIC_VIDEO")
    audio.write_bytes(b"SYNTHETIC_AUDIO")
    fake_binary = tmp_path / "ffmpeg.exe"
    fake_binary.write_bytes(b"NOT_EXECUTED")
    output = tmp_path / "merged.mp4"
    captured = []
    def fake_run(command, **kwargs):
        captured.append(command)
        # Simulate only the final output creation needed by actual merger.run.
        assert command[-1].startswith("file:")
        Path(command[-1][5:]).write_bytes(b"SYNTHETIC_MERGED")
        return "", "", 0
    monkeypatch.setattr(upstream.Popen, "run", fake_run)
    with YoutubeDL({"quiet": True, "logger": probe.video.StopLogger(), "ffmpeg_location": str(fake_binary),
                    "postprocessor_args": probe.merger_postprocessor_args()}, auto_init=False) as downloader:
        merger = upstream.FFmpegMergerPP(downloader)
        merger.basename = "ffmpeg"
        monkeypatch.setattr(merger, "check_version", lambda: None)
        merger.run({"filepath": str(output), "ext": "mp4", "__files_to_merge": [str(visual), str(audio)],
                    "requested_formats": [{"vcodec": "h264", "acodec": "none", "protocol": "https"},
                                          {"vcodec": "none", "acodec": "aac", "protocol": "https"}]})
    assert len(captured) == 1
    command = captured[0]
    indices = [index for index, arg in enumerate(command) if arg == "-i"]
    assert len(indices) == 2
    for index in indices:
        assert command[index - 4:index] == ["-protocol_whitelist", "file,pipe", "-f", "mov"]
        assert command[index + 1].startswith("file:")
    assert output.read_bytes() == b"SYNTHETIC_MERGED"


def test_logged_out_does_not_start_download(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "check_workdir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(probe, "check_ffmpeg", lambda *a: (Path("ffmpeg.exe"), Path("ffprobe.exe")))
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda *a: probe.video.PINNED_VERSION)
    monkeypatch.setattr(probe.common, "load_bili_cookies", lambda *a: (http.cookiejar.CookieJar(), {}))
    monkeypatch.setattr(probe.common, "run_account", lambda *a: {"account": "BLOCKED", "stop_reason": "not_logged_in"})
    monkeypatch.setattr(probe, "download_one", lambda *a: pytest.fail("Download attempted"))
    result = probe.run_worker("SYNTHETIC", tmp_path, Path("ffmpeg.exe"))
    assert result["media_attempts"] == 0


def test_worker_stops_after_first_failure_and_preserves_success(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "check_workdir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(probe, "check_ffmpeg", lambda *a: (Path("ffmpeg.exe"), Path("ffprobe.exe")))
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda *a: probe.video.PINNED_VERSION)
    monkeypatch.setattr(probe.common, "load_bili_cookies", lambda *a: (http.cookiejar.CookieJar(), {}))
    monkeypatch.setattr(probe.common, "run_account", lambda *a: {"account": "PASS"})
    calls = []
    def download(sample, *args):
        calls.append(sample)
        if len(calls) == 2:
            raise probe.ProbeStop("platform_stop")
        return {"download": "PASS"}
    monkeypatch.setattr(probe, "download_one", download)
    result = probe.run_worker("SYNTHETIC", tmp_path, Path("ffmpeg.exe"))
    assert len(calls) == 2 and len(result["assets"]) == 1
    assert result["download"] == "PARTIAL"


def test_child_cannot_start_work_before_job_assignment(monkeypatch):
    events = []
    class Job:
        def assign(self, child):
            events.append("assigned")
        def close(self):
            events.append("job_closed")
    class Child:
        returncode = 0
        def communicate(self, input, timeout):
            assert events == ["spawned", "assigned"]
            assert input == "START_U02\n" and timeout <= 230
            events.append("started")
            return '{"download":"PASS"}', None
        def poll(self):
            return 0
    def spawn(*args, **kwargs):
        assert kwargs["stdin"] == subprocess.PIPE
        events.append("spawned")
        return Child()
    monkeypatch.setattr(probe, "WindowsJob", Job)
    monkeypatch.setattr(probe.subprocess, "Popen", spawn)
    assert probe.run_bounded("SYNTHETIC", Path("workdir"), Path("ffmpeg.exe"))["download"] == "PASS"
    assert events[-1] == "job_closed"


def test_parent_report_does_not_persist_sensitive_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    (tmp_path / ".artifacts").mkdir()
    monkeypatch.setattr(probe, "check_ffmpeg", lambda *a: (Path("ffmpeg.exe"), Path("ffprobe.exe")))
    def fail(*args):
        raise RuntimeError("SYNTHETIC_COOKIE_PRIVATE_PATH_SIGNED_URL")
    monkeypatch.setattr(probe, "run_bounded", fail)
    workdir = tmp_path / ".artifacts" / "video-download-report"
    assert probe.main(["--profile", "PRIVATE_PROFILE", "--workdir", str(workdir), "--ffmpeg", "ffmpeg.exe"]) == 2
    text = (workdir / "report.json").read_text()
    assert "PRIVATE" not in text and "SIGNED_URL" not in text and "SYNTHETIC_COOKIE" not in text
    assert json.loads(text)["research_gate"] == "NEEDS_MORE_EVIDENCE"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration")
def test_windows_job_kills_descendant_but_not_unrelated_process():
    job = probe.WindowsJob()
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], creationflags=subprocess.CREATE_NO_WINDOW)
    worker, child_handle = None, None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        code = "import sys,subprocess,time; sys.stdin.readline(); p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); print(p.pid,flush=True); time.sleep(20)"
        worker = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(worker)
        worker.stdin.write("START\n")
        worker.stdin.flush()
        pid = int(worker.stdout.readline().strip())
        child_handle = kernel.OpenProcess(0x00100000, False, pid)
        assert child_handle
        assert kernel.WaitForSingleObject(child_handle, 0) == 258  # WAIT_TIMEOUT: alive before close.
        job.close()
        worker.wait(timeout=5)
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0  # Only this job's descendant is dead.
        assert unrelated.poll() is None
    finally:
        job.close()
        if worker and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        if child_handle:
            kernel.CloseHandle(child_handle)
        unrelated.kill()
        unrelated.wait(timeout=5)
