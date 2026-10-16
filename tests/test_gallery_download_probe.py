import hashlib
import gzip
import http.cookiejar
import io
import json
import os
from email.message import Message
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
    monkeypatch.setattr(probe, "load_bili_cookies", blocked)


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


def baseline(dynamic_id=STILL, rows=None):
    rows = assets(dynamic_id) if rows is None else rows
    return {"author_uid": probe.AUTHOR_UID, "sha256": "a" * 64, "assets": [
        {"sequence": row["sequence"], "suffix": "l" if row["pair_role"] == "live_video" else "",
         "format": row["original_extension"], "source_path_sha256": row["source_path_sha256"]} for row in rows]}


def write_baseline(tmp_path, dynamic_id=STILL):
    data = {"experiment": "U03_gallery_metadata", "status": "METADATA_EXTRACTED_NOT_MEDIA_VERIFIED",
            "sample_url": probe.source_url(dynamic_id), "gallery_dl": probe.PINNED_GALLERY,
            "author_uid": probe.AUTHOR_UID, "declared_images": probe.SAMPLES[dynamic_id]["images"],
            "assets": baseline(dynamic_id)["assets"]}
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


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
        probe.main(["--profile", PRIVATE, "--sample", STILL, "--workdir", str(target), "--ffmpeg", str(ffmpeg),
                    "--baseline-report", str(tmp_path / "not-read-before-tool-check.json")])
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
            yield (2, "", {"user_id": int(probe.AUTHOR_UID), "count": 9})
            for row in assets(LIVE):
                yield (3, row["url"], {"num": row["sequence"], "suffix": "l" if row["pair_role"] == "live_video" else ""})
    monkeypatch.setattr(extractor, "find", lambda _: BilibiliArticleExtractor())
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _: probe.PINNED_GALLERY)
    rows, identity = probe.discover_assets(LIVE, instance, baseline(LIVE))
    assert identity == probe.AUTHOR_UID and len(rows) == 18 and len(calls) == 1


def test_local_decode_records_only_allowed_fields(monkeypatch, tmp_path):
    data = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 480, "tags": PRIVATE},
                         {"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": "2.52", "filename": PRIVATE}}
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="frame=63\nprogress=end\n" if "-progress" in command else json.dumps(data))
    monkeypatch.setattr(probe.subprocess, "run", run)
    report = probe.inspect_asset(tmp_path / "sample.mp4", "mp4", Path("ffmpeg.exe"), Path("ffprobe.exe"), time.monotonic() + 180)
    assert report["decode"] == "PASS" and report["duration_seconds"] == 2.52 and report["decoded_frames"] == 63
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
    monkeypatch.setattr(probe, "discover_assets", lambda *args: (rows, probe.AUTHOR_UID))
    def fail(*args):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(probe.Transport, "download", fail)
    baseline_path = write_baseline(tmp_path)
    report = probe.run_worker(PRIVATE, STILL, tmp_path, Path("ffmpeg.exe"), baseline_path,
                              hashlib.sha256(baseline_path.read_bytes()).hexdigest())
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
            probe.run_bounded(PRIVATE, STILL, tmp_path, Path("ffmpeg.exe"), tmp_path / "baseline.json", "a" * 64)
        assert "start" not in events and "killed" in events
    else:
        report = probe.run_bounded(PRIVATE, STILL, tmp_path, Path("ffmpeg.exe"), tmp_path / "baseline.json", "a" * 64)
        assert report["download"] == ("BLOCKED" if scenario == "timeout" else "PASS")
        if scenario == "timeout":
            assert report["stop_reason"] == "worker_total_timeout"
    assert "closed" in events


def test_worker_requires_parent_start_token_before_work(monkeypatch):
    monkeypatch.setattr(probe.sys, "stdin", io.StringIO("wrong\n"))
    monkeypatch.setattr(probe, "run_worker", lambda *args: pytest.fail("worker started without handshake"))
    assert probe.main(["--worker", "--profile", PRIVATE, "--sample", STILL, "--workdir", "ignored", "--ffmpeg", "ignored",
                       "--baseline-report", "ignored"]) == 2


def synthetic_page(dynamic_id=STILL, *, uid=probe.AUTHOR_UID, blocked=False):
    rows = assets(dynamic_id)
    pictures = []
    for row in rows:
        if row["pair_role"] == "image":
            pictures.append({"url": row["url"], "width": 32, "height": 24})
        else:
            pictures[-1]["live_url"] = row["url"]
    modules = [{"module_type": "MODULE_AUTHOR", "module_author": {"mid": uid, "name": "Synthetic", "pub_ts": 1}},
               {"module_type": "MODULE_TOP", "module_top": {"display": {"album": {"pics": pictures}}}}]
    if blocked:
        modules.append({"module_type": "MODULE_BLOCKED", "module_blocked": {"hint_message": PRIVATE}})
    return ("<html><script>window.__INITIAL_STATE__=" + json.dumps({"id": dynamic_id, "detail": {"modules": modules}})
            + ";</script></html>").encode()


@pytest.mark.parametrize("dynamic_id", [STILL, LIVE])
def test_real_upstream_extractor_consumes_decoded_page_and_matches_baseline(dynamic_id, monkeypatch):
    page = synthetic_page(dynamic_id)
    raw = gzip.compress(page, mtime=0)
    instance, calls = transport(monkeypatch, [Reply(raw, headers={"Content-Encoding": "GZip"})], dynamic_id)
    actual, uid = probe.discover_assets(dynamic_id, instance, baseline(dynamic_id))
    assert uid == probe.AUTHOR_UID and actual == assets(dynamic_id)
    assert len(calls) == 1 and instance.media_requests == 0
    assert instance.page_sha256 == hashlib.sha256(raw).hexdigest()
    assert instance.page_decoding == {"status": "DECODED", "encoding": "GZIP", "raw_bytes": len(raw),
        "decoded_bytes": len(page), "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "decoded_sha256": hashlib.sha256(page).hexdigest()}
    assert PRIVATE not in json.dumps(instance.page_decoding)


@pytest.mark.parametrize("marker,reason", [(b"window._riskdata_", "verification_page_detected"),
                                          (b"window.__riskdata__", "verification_page_detected"),
                                          (b'"module_blocked"', "blocked_gallery_module")])
@pytest.mark.parametrize("compressed", [False, True])
def test_risk_and_blocked_pages_stop_before_gallery_or_media(marker, reason, compressed, monkeypatch):
    raw = gzip.compress(marker, mtime=0) if compressed else marker
    instance, calls = transport(monkeypatch, [Reply(raw, headers={"Content-Encoding": "gzip"} if compressed else {})])
    with pytest.raises(probe.ProbeStop, match=reason):
        instance.fetch_page(probe.source_url(STILL))
    assert len(calls) == 1 and instance.media_requests == 0
    assert instance.page_decoding["status"] == ("DECODED" if compressed else "PASS_THROUGH")


def test_structural_blocked_module_cannot_hide_behind_json_unicode_escape(monkeypatch):
    page = synthetic_page(blocked=True).replace(b'"module_blocked"', b'"module_\\u0062locked"')
    instance, calls = transport(monkeypatch, [Reply(page)])
    with pytest.raises(probe.ProbeStop, match="blocked_gallery_module"):
        probe.discover_assets(STILL, instance, baseline())
    assert len(calls) == 1 and instance.media_requests == 0


@pytest.mark.parametrize("raw,headers,reason", [
    (b"not-gzip", {"Content-Encoding": "gzip"}, "metadata_gzip_invalid"),
    (b"x" * (probe.MAX_PAGE_BYTES + 1), {}, "metadata_size_limit"),
    (gzip.compress(b"x" * (probe.MAX_PAGE_BYTES + 1), mtime=0), {"Content-Encoding": "gzip"}, "metadata_decoded_size_limit"),
    (b"page", {"Content-Encoding": "br"}, "content_encoding_unsupported")],
    ids=["invalid-gzip", "raw-over-limit", "decoded-over-limit", "unsupported-encoding"])
def test_page_decoding_limits_are_wired_to_shared_decoder(raw, headers, reason, monkeypatch):
    instance, calls = transport(monkeypatch, [Reply(raw, headers=headers)])
    with pytest.raises(probe.ProbeStop, match=reason):
        instance.fetch_page(probe.source_url(STILL))
    assert len(calls) == 1 and instance.media_requests == 0
    assert instance.page_decoding["status"] == "BLOCKED" and instance.page_decoding["reason"] == reason
    if reason == "metadata_size_limit":
        assert instance.page_sha256 is None and instance.page_decoding["raw_complete"] is False
        assert "raw_sha256" not in instance.page_decoding and "decoded_sha256" not in instance.page_decoding
        assert instance.page_decoding["raw_prefix_sha256"] == hashlib.sha256(raw).hexdigest()
    else:
        assert instance.page_decoding["raw_complete"] is True
        assert instance.page_decoding["raw_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("encodings", [("identity", "identity"), ("",), ("br",), ("gzip",), ("identity, gzip",)])
def test_media_encoding_cannot_borrow_page_gzip_support(encodings, tmp_path, monkeypatch):
    headers = Message()
    for encoding in encodings:
        headers["Content-Encoding"] = encoding
    instance, calls = transport(monkeypatch, [Reply(b"original", headers=headers)])
    row = assets()[0]
    instance.allow_assets([row])
    with pytest.raises(probe.ProbeStop, match="unexpected_content_encoding"):
        instance.download(row, tmp_path / row["filename"])
    assert len(calls) == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("dynamic_id,limit", [(STILL, 6), (LIVE, 20)])
def test_per_sample_request_budget_includes_account_source_and_all_assets(dynamic_id, limit, tmp_path, monkeypatch):
    rows = assets(dynamic_id)
    instance, calls = transport(monkeypatch, [Reply(b"page")] + [Reply(b"media") for _ in rows], dynamic_id)
    instance.fetch_page(probe.source_url(dynamic_id))
    instance.allow_assets(rows)
    for row in rows:
        instance.download(row, tmp_path / row["filename"])
    assert instance.request_limit == instance.count == limit and len(calls) == limit - 1
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        instance.open(probe.source_url(dynamic_id), media=False)
    assert len(calls) == limit - 1


def test_account_to_source_interval_is_at_least_one_second(monkeypatch):
    clock = [100.25]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(probe.time, "sleep", sleep)
    instance = probe.Transport(http.cookiejar.CookieJar(), STILL, 280, account_requests=1, last_request_at=100)
    def open_page(request, timeout):
        assert clock[0] >= 101 and timeout == 15
        return Reply(b"page")
    monkeypatch.setattr(instance.page_opener, "open", open_page)
    instance.fetch_page(probe.source_url(STILL))
    assert sleeps == [0.75] and instance.count == 2


@pytest.mark.parametrize("dynamic_id", [STILL, LIVE])
def test_baseline_report_is_bound_to_original_content_identity(dynamic_id, tmp_path):
    path = write_baseline(tmp_path, dynamic_id)
    result = probe.load_baseline(path, dynamic_id)
    assert result["author_uid"] == probe.AUTHOR_UID and result["assets"] == baseline(dynamic_id)["assets"]
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    probe.validate_baseline_assets(assets(dynamic_id), probe.AUTHOR_UID, result)


@pytest.mark.parametrize("change", ["sample", "uid", "status", "version", "declared", "order", "format", "hash", "duplicate_hash"])
def test_invalid_baseline_report_is_rejected_without_network(change, tmp_path):
    path = write_baseline(tmp_path)
    data = json.loads(path.read_text())
    if change == "sample": data["sample_url"] = probe.source_url(LIVE)
    elif change == "uid": data["author_uid"] = "123"
    elif change == "status": data["status"] = "FAILED"
    elif change == "version": data["gallery_dl"] = "0.0"
    elif change == "declared": data["declared_images"] = True
    elif change == "order": data["assets"].reverse()
    elif change == "format": data["assets"][0]["format"] = "mp4"
    elif change == "hash": data["assets"][0]["source_path_sha256"] = PRIVATE
    else: data["assets"][1]["source_path_sha256"] = data["assets"][0]["source_path_sha256"]
    path.write_text(json.dumps(data))
    with pytest.raises(probe.ProbeStop, match="baseline_report_invalid") as stopped:
        probe.load_baseline(path, STILL)
    assert PRIVATE not in str(stopped.value.evidence)


@pytest.mark.parametrize("change", ["uid", "order", "format", "path"])
def test_discovered_content_must_match_fixed_baseline(change):
    rows, uid = assets(), probe.AUTHOR_UID
    if change == "uid": uid = "123"
    elif change == "order": rows.reverse()
    elif change == "format": rows[0]["original_extension"] = "jpg"
    else: rows[0]["source_path_sha256"] = "f" * 64
    with pytest.raises(probe.ProbeStop, match="fixed_sample_baseline_mismatch"):
        probe.validate_baseline_assets(rows, uid, baseline())


def test_signed_query_refresh_does_not_change_source_path_identity():
    original = assets()[0]
    changed = probe.asset_descriptor(STILL, original["url"].replace(PRIVATE, "CHANGED_QUERY"), {"num": 1})
    assert original["source_path_sha256"] == changed["source_path_sha256"]
    rows = assets()
    rows[0] = changed
    probe.validate_baseline_assets(rows, probe.AUTHOR_UID, baseline())


def test_missing_baseline_is_cli_error_before_creds_or_output(tmp_path, monkeypatch, capsys):
    target = tmp_path / "gallery-download-not-created"
    monkeypatch.setattr(probe, "run_bounded", lambda *a: pytest.fail("Worker started without baseline"))
    with pytest.raises(SystemExit) as stopped:
        probe.main(["--profile", PRIVATE, "--sample", STILL, "--workdir", str(target), "--ffmpeg", "ignored"])
    assert stopped.value.code == 2 and "--baseline-report" in capsys.readouterr().err
    assert not target.exists()


def test_worker_rejects_changed_baseline_before_loading_credentials(tmp_path, monkeypatch):
    baseline_path = write_baseline(tmp_path)
    fingerprint = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    baseline_path.write_text(baseline_path.read_text() + " ")  # Same JSON, different recorded evidence.
    monkeypatch.setattr(probe, "check_workdir", lambda path, create: path)
    monkeypatch.setattr(probe, "check_ffmpeg", lambda path: (path, path))
    result = probe.run_worker(PRIVATE, STILL, tmp_path, Path("ffmpeg.exe"), baseline_path, fingerprint)
    assert result["stop_reason"] == "baseline_report_changed_after_preflight" and result["network_requests"] == 0


def test_worker_requires_parent_baseline_fingerprint(monkeypatch):
    monkeypatch.setattr(probe.sys, "stdin", io.StringIO(probe.START_TOKEN))
    monkeypatch.setattr(probe, "run_worker", lambda *args: pytest.fail("Worker started without baseline fingerprint"))
    assert probe.main(["--worker", "--profile", PRIVATE, "--sample", STILL, "--workdir", "ignored", "--ffmpeg", "ignored",
                       "--baseline-report", "ignored"]) == 2


@pytest.mark.parametrize("frames,ended,reason", [(0, True, "insufficient_decoded_frames"),
    (1, True, "insufficient_decoded_frames"), (2, False, "local_decode_summary_missing")])
def test_live_video_needs_two_actually_decoded_frames_and_completion(frames, ended, reason, monkeypatch, tmp_path):
    data = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 32, "height": 24}], "format": {"duration": "1"}}
    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=(f"frame={frames}\nprogress={'end' if ended else 'continue'}\n"
                                                     if "-progress" in command else json.dumps(data)))
    monkeypatch.setattr(probe.subprocess, "run", run)
    with pytest.raises(probe.ProbeStop, match=reason):
        probe.inspect_asset(tmp_path / "synthetic.mp4", "mp4", Path("ffmpeg.exe"), Path("ffprobe.exe"), time.monotonic() + 180)


def test_live_video_with_two_decoded_frames_does_not_require_audio(monkeypatch, tmp_path):
    data = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 32, "height": 24}], "format": {"duration": "1"}}
    def run(command, **kwargs):
        if "-progress" in command:
            assert "0:a?" in command
            assert command[command.index("-fps_mode") + 1] == "passthrough"
        return SimpleNamespace(returncode=0, stdout="frame=2\nprogress=end\n" if "-progress" in command else json.dumps(data))
    monkeypatch.setattr(probe.subprocess, "run", run)
    result = probe.inspect_asset(tmp_path / "synthetic.mp4", "mp4", Path("ffmpeg.exe"), Path("ffprobe.exe"), time.monotonic() + 180)
    assert result["decoded_frames"] == 2 and len(result["streams"]) == 1


@pytest.mark.parametrize("extension,frames", [("png", 1), ("jpg", 1), ("mp4", 3), ("mp4", 1)])
def test_real_ffmpeg_synthetic_original_images_and_live_video(extension, frames, tmp_path):
    binary = os.environ.get("BILI_TEST_FFMPEG")
    if not binary:
        pytest.skip("Set BILI_TEST_FFMPEG to the verified local FFmpeg for synthetic integration")
    ffmpeg, ffprobe = probe.check_ffmpeg(Path(binary))
    target = tmp_path / f"synthetic-{frames}.{extension}"
    command = [str(ffmpeg), "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=32x24:rate=3",
               "-frames:v", str(frames), "-an"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] if extension == "mp4" else ["-update", "1"]
    created = subprocess.run([*command, str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert created.returncode == 0, "Synthetic FFmpeg fixture generation failed"
    if extension == "mp4" and frames == 1:
        with pytest.raises(probe.ProbeStop, match="insufficient_decoded_frames"):
            probe.inspect_asset(target, extension, ffmpeg, ffprobe, time.monotonic() + 180)
    else:
        result = probe.inspect_asset(target, extension, ffmpeg, ffprobe, time.monotonic() + 180)
        assert result["decoded_frames"] == frames and result["decode"] == "PASS"
        assert [stream["codec_type"] for stream in result["streams"]] == ["video"]
