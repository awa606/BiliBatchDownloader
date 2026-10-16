import hashlib
import http.cookiejar
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "experiments"))
import gallery_download_probe as probe

STILL = "1249456774436618276"
LIVE = "1257912749013336118"
PRIVATE = "SYNTHETIC_PRIVATE_TOKEN"


@pytest.fixture(autouse=True)
def prohibit_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Offline test attempted network")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", blocked)


def assets(dynamic_id=STILL):
    contract = probe.SAMPLES[dynamic_id]
    result = []
    for number in range(1, contract["images"] + 1):
        result.append(probe.asset_descriptor(dynamic_id,
            f"https://i0.hdslb.com/bfs/image{number}.{contract['image_ext']}?token={PRIVATE}",
            {"num": number, "suffix": ""}))
        if contract["live"]:
            result.append(probe.asset_descriptor(dynamic_id,
                f"https://upos-sz-mirrorcos.bilivideo.com/live{number}.mp4?token={PRIVATE}",
                {"num": number, "suffix": "l"}))
    return result


class Reply(io.BytesIO):
    def __init__(self, body=b"PNG", status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


def transport(monkeypatch, replies, dynamic_id=STILL):
    instance = probe.Transport(http.cookiejar.CookieJar(), dynamic_id, time.monotonic() + 180)
    calls = []
    def open_response(request, timeout):
        calls.append(request)
        assert 0 < timeout <= 15
        result = replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(instance.page_opener, "open", open_response)
    monkeypatch.setattr(instance.media_opener, "open", open_response)
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    return instance, calls


@pytest.mark.parametrize("value", ["123", "https://evil.test/opus/" + STILL,
    "https://www.bilibili.com/opus/" + STILL + "?token=" + PRIVATE,
    "https://www.bilibili.com/opus/" + STILL + "/", "file:///secret"])
def test_only_two_preregistered_samples(value):
    with pytest.raises(probe.ProbeStop, match="sample_not_preregistered"):
        probe.sample_id(value)


def test_fixed_samples_and_aliases():
    assert probe.sample_id("https://t.bilibili.com/" + STILL) == STILL
    assert probe.sample_id(LIVE) == LIVE


def test_new_direct_artifacts_directory_only(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    (tmp_path / ".artifacts").mkdir()
    target = tmp_path / ".artifacts" / "gallery-download-case"
    assert probe.check_workdir(target, create=True) == target
    with pytest.raises(FileExistsError):
        probe.check_workdir(target, create=True)
    for invalid in (tmp_path / "gallery-download-out", target / "gallery-download-nested", tmp_path / ".artifacts" / "wrong"):
        with pytest.raises(probe.ProbeStop):
            probe.check_workdir(invalid, create=True)


def test_real_shared_tool_validation_requires_gallery_ffprobe(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"SYNTHETIC_NOT_EXECUTABLE")
    assert probe.shared_process.check_ffmpeg(ffmpeg) == (ffmpeg, None)
    with pytest.raises(probe.ProbeStop, match="ffprobe_required_for_gallery_verification"):
        probe.check_ffmpeg(ffmpeg)
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(b"SYNTHETIC_NOT_EXECUTABLE")
    assert probe.check_ffmpeg(ffmpeg) == (ffmpeg, ffprobe)


def test_missing_ffprobe_stops_cli_before_credentials_network_or_evidence(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    (tmp_path / ".artifacts").mkdir()
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"SYNTHETIC_NOT_EXECUTABLE")
    target = tmp_path / ".artifacts" / "gallery-download-preflight"
    monkeypatch.setattr(probe, "load_bili_cookies", lambda *a: pytest.fail("Credentials accessed before prerequisite gate"))
    monkeypatch.setattr(probe, "run_bounded", lambda *a: pytest.fail("Worker started before prerequisite gate"))
    with pytest.raises(SystemExit) as error:
        probe.main(["--profile", PRIVATE, "--sample", STILL, "--workdir", str(target), "--ffmpeg", str(ffmpeg)])
    assert error.value.code == 2
    assert "ffprobe_required_for_gallery_verification" in capsys.readouterr().err
    assert not target.exists()


@pytest.mark.parametrize("url", ["http://i0.hdslb.com/x.png", "https://i0.hdslb.com.evil.test/x.png",
    "https://user:secret@i0.hdslb.com/x.png", "https://i0.hdslb.com:444/x.png", "https://127.0.0.1/x.png",
    "https://i0.hdslb.com/x.exe", "https://i0.hdslb.com/x.png#fragment", "https://evil.hdslb.com/x.png",
    "https://bilivideo.com.evil.test/x.mp4", "https://a..bilivideo.com/x.mp4", "https://-a.bilivideo.com/x.mp4",
    "https://upos.bilivideo.com/image.jpg", "https://bilivideo.com/x.mp4"])
def test_cdn_origin_boundary(url):
    with pytest.raises(probe.ProbeStop, match="media_origin_or_extension_not_allowed"):
        probe.media_extension(url)


def test_approved_video_cdn_and_original_pair_order():
    paired = assets(LIVE)
    probe.validate_assets(LIVE, paired, 9)
    assert len(paired) == 18
    assert [(a["sequence"], a["pair_role"]) for a in paired[:4]] == [(1, "image"), (1, "live_video"), (2, "image"), (2, "live_video")]
    assert paired[0]["filename"] == LIVE + "_001.jpg"
    assert paired[1]["filename"] == LIVE + "_001l.mp4"
    assert probe.media_extension("https://upos-sz-mirrorali.bilivideo.cn/x.mp4") == "mp4"


@pytest.mark.parametrize("metadata", [{"num": True}, {"num": 0}, {"num": 21}, {"num": "1"}, {"num": 1, "suffix": "../../x"}, {"num": 1, "suffix": "l"}])
def test_filename_sequence_and_pair_kind_are_fixed(metadata):
    with pytest.raises(probe.ProbeStop):
        probe.asset_descriptor(STILL, "https://i0.hdslb.com/image.png", metadata)


@pytest.mark.parametrize("change", ["count", "reorder", "missing", "duplicate"])
def test_changed_fixed_sample_fails_before_download(change):
    rows = assets(LIVE)
    declared = 9
    if change == "count":
        declared = 8
    elif change == "reorder":
        rows.reverse()
    elif change == "missing":
        rows.pop()
    else:
        rows[1] = rows[0]
    with pytest.raises(probe.ProbeStop, match="fixed_sample_contract_changed"):
        probe.validate_assets(LIVE, rows, declared)


def test_source_once_and_no_copied_credentials_to_media(tmp_path, monkeypatch):
    instance, calls = transport(monkeypatch, [Reply(b"page"), Reply(b"original", headers={"Content-Length": "8"})])
    assert instance.fetch_page(probe.source_url(STILL)).text == "page"
    with pytest.raises(probe.ProbeStop, match="source_request_budget_exhausted"):
        instance.fetch_page(probe.source_url(STILL))
    rows = assets()
    instance.allow_assets(rows)
    destination = tmp_path / rows[0]["filename"]
    evidence = instance.download(rows[0], destination)
    assert evidence == {"size_bytes": 8, "sha256": hashlib.sha256(b"original").hexdigest()}
    assert destination.read_bytes() == b"original"
    assert instance.count == 3
    assert not calls[-1].has_header("Cookie") and not calls[-1].has_header("Authorization")
    assert not any(isinstance(h, urllib.request.HTTPCookieProcessor) for h in instance.media_opener.handlers)
    assert PRIVATE not in json.dumps(evidence)
    with pytest.raises(probe.ProbeStop, match="media_not_preregistered"):
        instance.download(rows[0], destination)


def test_redirect_fails_once_without_retry(tmp_path, monkeypatch):
    error = urllib.error.HTTPError("https://hidden.test/" + PRIVATE, 302, PRIVATE,
                                  {"Location": "https://i1.hdslb.com/redirect.png"}, io.BytesIO())
    instance, calls = transport(monkeypatch, [error])
    rows = assets()
    instance.allow_assets(rows)
    with pytest.raises(probe.ProbeStop) as stopped:
        instance.download(rows[0], tmp_path / rows[0]["filename"])
    assert stopped.value.evidence == {"http_status": 302}
    assert PRIVATE not in str(stopped.value.evidence)
    assert len(calls) == 1
    assert not list(tmp_path.iterdir())


def test_no_redirect_handler_refuses_same_host():
    with pytest.raises(probe.ProbeStop, match="redirect_blocked"):
        probe.NoRedirect().redirect_request(None, None, 302, "secret", {}, "https://i0.hdslb.com/x.png")


@pytest.mark.parametrize("headers", [{"Content-Length": "999999999"}, {"Content-Length": "not-a-number"}, {"Content-Length": "0"}, {"Content-Encoding": "gzip"}])
def test_header_caps_before_file_write(tmp_path, monkeypatch, headers):
    instance, _ = transport(monkeypatch, [Reply(b"x", headers=headers)])
    row = assets()[0]
    instance.allow_assets([row])
    with pytest.raises(probe.ProbeStop):
        instance.download(row, tmp_path / row["filename"])
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("limit", ["asset", "total"])
def test_streaming_caps_even_without_content_length(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(probe, "MAX_ASSET_BYTES" if limit == "asset" else "MAX_TOTAL_BYTES", 4)
    instance, _ = transport(monkeypatch, [Reply(b"12345678")])
    row = assets()[0]
    instance.allow_assets([row])
    destination = tmp_path / row["filename"]
    with pytest.raises(probe.ProbeStop, match="media_byte_budget_exhausted"):
        instance.download(row, destination)
    assert destination.read_bytes() == b"1234"
    assert instance.total_bytes == 4


def test_existing_file_never_overwritten(tmp_path, monkeypatch):
    instance, _ = transport(monkeypatch, [Reply(b"new")])
    row = assets()[0]
    instance.allow_assets([row])
    destination = tmp_path / row["filename"]
    destination.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        instance.download(row, destination)
    assert destination.read_bytes() == b"old"


def test_expired_deadline_makes_zero_requests(monkeypatch):
    instance, calls = transport(monkeypatch, [])
    instance.deadline = 0
    with pytest.raises(probe.ProbeStop, match="worker_total_timeout"):
        instance.fetch_page(probe.source_url(STILL))
    assert not calls and instance.count == 1


def test_request_limit_and_unknown_asset(tmp_path, monkeypatch):
    instance, calls = transport(monkeypatch, [])
    with pytest.raises(probe.ProbeStop, match="media_not_preregistered"):
        instance.download(assets()[0], tmp_path / "not-created.png")
    instance.count = 22
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        instance.fetch_page(probe.source_url(STILL))
    assert not calls


def test_discovery_uses_only_bounded_source_request(monkeypatch):
    from gallery_dl import extractor
    instance, calls = transport(monkeypatch, [Reply(b"synthetic page")], LIVE)
    class BilibiliArticleExtractor:
        groups = (LIVE,)
        def initialize(self):
            pass
        def __iter__(self):
            self.request(probe.source_url(LIVE))
            yield (2, "", {"user_id": 123, "count": 9})
            for row in assets(LIVE):
                yield (3, row["url"], {"num": row["sequence"], "suffix": "l" if row["pair_role"] == "live_video" else ""})
    monkeypatch.setattr(extractor, "find", lambda _: BilibiliArticleExtractor())
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _: probe.PINNED_GALLERY)
    rows, identity = probe.discover_assets(LIVE, instance)
    assert identity == "123" and len(rows) == 18 and len(calls) == 1


def test_local_decode_records_only_allowed_fields(monkeypatch, tmp_path):
    data = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 480, "tags": PRIVATE},
                         {"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": "2.52", "filename": PRIVATE}}
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))
    monkeypatch.setattr(probe.subprocess, "run", run)
    report = probe.inspect_asset(tmp_path / "sample.mp4", "mp4", Path("ffmpeg.exe"), Path("ffprobe.exe"), time.monotonic() + 180)
    assert report["decode"] == "PASS" and report["duration_seconds"] == 2.52
    assert len(calls) == 2 and all(kw["stderr"] == subprocess.DEVNULL for _, kw in calls)
    assert "null" in calls[1][0] and "-xerror" in calls[1][0]
    assert PRIVATE not in json.dumps(report)


def test_image_original_codec_mismatch_never_decodes(monkeypatch, tmp_path):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "video", "codec_name": "mjpeg", "width": 10, "height": 10}]}))
    monkeypatch.setattr(probe.subprocess, "run", run)
    with pytest.raises(probe.ProbeStop, match="original_format_mismatch"):
        probe.inspect_asset(tmp_path / "sample.png", "png", Path("ffmpeg.exe"), Path("ffprobe.exe"), time.monotonic() + 180)
    assert len(calls) == 1


def test_worker_does_not_persist_urls_or_private_error_text(monkeypatch, tmp_path):
    rows = assets()
    monkeypatch.setattr(probe, "check_workdir", lambda path, create: path)
    monkeypatch.setattr(probe, "check_ffmpeg", lambda path: (path, path))
    monkeypatch.setattr(probe, "load_bili_cookies", lambda *args: (http.cookiejar.CookieJar(), {}))
    monkeypatch.setattr(probe, "run_account", lambda get: {"account": "PASS"})
    monkeypatch.setattr(probe, "discover_assets", lambda *args: (rows, "123"))
    def fail(*args):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(probe.Transport, "download", fail)
    report = probe.run_worker(PRIVATE, STILL, tmp_path, Path("ffmpeg.exe"))
    assert report["download"] == "BLOCKED"
    assert report["research_gate"] == "NEEDS_MORE_EVIDENCE"
    assert PRIVATE not in json.dumps(report) and "https://" not in json.dumps(report)
    assert report["stop_evidence"] == {"exception_type": "RuntimeError"}


@pytest.mark.parametrize("scenario", ["assignment_failure", "timeout", "success"])
def test_parent_job_assignment_and_timeout_lifecycle(monkeypatch, tmp_path, scenario):
    events = []
    class Job:
        def __init__(self):
            events.append("created")
        def assign(self, process):
            events.append("assigned")
            if scenario == "assignment_failure":
                raise probe.ProbeStop("job_assignment_failed")
        def close(self):
            events.append("closed")
    class Process:
        returncode = None
        count = 0
        def communicate(self, input=None, timeout=None):
            self.count += 1
            if input:
                events.append("start")
                assert input == probe.START_TOKEN and events.index("assigned") < events.index("start")
            if scenario == "timeout" and self.count == 1:
                raise subprocess.TimeoutExpired("hidden", timeout)
            self.returncode = 0
            return (json.dumps({"download": "PASS"}), "")
        def poll(self):
            return self.returncode
        def kill(self):
            events.append("killed")
        def wait(self, timeout):
            events.append("waited")
            self.returncode = 1
    process = Process()
    monkeypatch.setattr(probe, "WindowsJob", Job)
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: process)
    if scenario == "assignment_failure":
        with pytest.raises(probe.ProbeStop):
            probe.run_bounded(PRIVATE, STILL, tmp_path, Path("ffmpeg.exe"))
        assert "start" not in events and "killed" in events
    else:
        report = probe.run_bounded(PRIVATE, STILL, tmp_path, Path("ffmpeg.exe"))
        assert report["download"] == ("BLOCKED" if scenario == "timeout" else "PASS")
        if scenario == "timeout":
            assert report["stop_reason"] == "worker_total_timeout"
    assert "closed" in events


def test_worker_requires_parent_start_token_before_work(monkeypatch):
    monkeypatch.setattr(probe.sys, "stdin", io.StringIO("wrong\n"))
    monkeypatch.setattr(probe, "run_worker", lambda *args: pytest.fail("worker started without handshake"))
    assert probe.main(["--worker", "--profile", PRIVATE, "--sample", STILL, "--workdir", "ignored", "--ffmpeg", "ignored"]) == 2
