import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location("local_preview_probe", Path(__file__).parents[1] / "experiments" / "local_preview_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.fixture
def source_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    artifacts = tmp_path / ".artifacts"
    artifacts.mkdir()
    folder = artifacts / "video-download-synthetic"
    folder.mkdir()
    assets = []
    for bvid, part, cid in probe.FIXED_ASSETS:
        relative = f"media/{bvid}/{cid}/asset.mp4"
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = b"SYNTHETIC_LOCAL_VIDEO_ONLY"
        path.write_bytes(content)
        assets.append({"bvid": bvid, "part": part, "cid": cid, "relative_file": relative,
                       "duration_seconds": 10.4, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                       "download": "PASS", "verification": "PASS", "full_decode": "PASS"})
    report = folder / "report.json"
    report.write_text(json.dumps({"experiment": "U02_VIDEO_DOWNLOAD", "download": "PASS", "assets": assets}), encoding="utf-8")
    return report, assets, artifacts


@pytest.mark.parametrize("duration,expected", [(0.1, [0.05]), (4, [2]), (5, [2.5]),
                                               (10, [2.5, 7.5]), (30, [2.5, 7.5, 12.5, 17.5, 22.5, 27.5])])
def test_midpoints_and_short_video_reduction(duration, expected):
    assert probe.sample_times(duration) == expected
    assert len(probe.sample_times(600)) == 6


@pytest.mark.parametrize("duration", [0, -1, True, float("nan"), float("inf"), "1"])
def test_invalid_duration(duration):
    with pytest.raises(probe.ProbeStop, match="invalid_duration"):
        probe.sample_times(duration)


def test_fixed_sources_verified_without_changing_report(source_fixture):
    path, _, _ = source_fixture
    before = path.read_bytes()
    digest, assets = probe.read_report(path)
    assert digest == hashlib.sha256(before).hexdigest()
    assert [(a["bvid"], a["part"], a["cid"]) for a in assets] == list(probe.FIXED_ASSETS)
    for asset in assets:
        assert probe.verify_source(path, asset).is_file()
    assert before == path.read_bytes()


@pytest.mark.parametrize("field,value", [("relative_file", "../../private.mp4"), ("cid", 1),
                                         ("part", True), ("sha256", "bad"), ("duration_seconds", True),
                                         ("verification", "FAIL"), ("full_decode", "NOT_TESTED")])
def test_report_rejects_unsafe_paths_and_wrong_identity(source_fixture, field, value):
    path, _, _ = source_fixture
    data = json.loads(path.read_text())
    data["assets"][0][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(probe.ProbeStop, match="source_report_contract_invalid"):
        probe.read_report(path)


def test_duplicate_identity_rejected(source_fixture):
    path, _, _ = source_fixture
    data = json.loads(path.read_text())
    data["assets"][2] = data["assets"][1]
    path.write_text(json.dumps(data))
    with pytest.raises(probe.ProbeStop):
        probe.read_report(path)


@pytest.mark.parametrize("mutation,reason", [("missing", "source_file_missing"), ("size", "source_size_mismatch"),
                                           ("hash", "source_sha256_mismatch")])
def test_source_missing_size_and_hash(source_fixture, mutation, reason):
    report, assets, _ = source_fixture
    source = report.parent / assets[0]["relative_file"]
    if mutation == "missing":
        source.unlink()
    elif mutation == "size":
        source.write_bytes(b"small")
    else:
        source.write_bytes(b"X" * assets[0]["size_bytes"])
    with pytest.raises(probe.ProbeStop, match=reason):
        probe.verify_source(report, assets[0])


def test_output_exclusive_and_outside_artifacts(source_fixture):
    _, _, artifacts = source_fixture
    output = artifacts / "local-preview-one"
    assert probe.output_path(output, create=True) == output
    with pytest.raises(FileExistsError):
        probe.output_path(output, create=True)
    with pytest.raises(probe.ProbeStop):
        probe.output_path(artifacts.parent / "local-preview-outside", create=True)
    with pytest.raises(probe.ProbeStop):
        probe.output_path(output / "local-preview-nested", create=True)


def test_link_escape_rejected_without_read(source_fixture):
    report, assets, artifacts = source_fixture
    source = report.parent / assets[0]["relative_file"]
    source.unlink()
    outside = artifacts.parent / "private.mp4"
    outside.write_bytes(b"PRIVATE_SYNTHETIC")
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks needs Windows developer mode")
    with pytest.raises(probe.ProbeStop, match="linked_path_not_allowed"):
        probe.verify_source(report, assets[0])


def png_header(width=320, height=180):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)


def showinfo(actual=3, pts=49152, width=320, height=180):
    return f"[Parsed_showinfo_2 @ 0x0] n:   0 pts: {pts} pts_time:{actual} duration: 1 s:{width}x{height} checksum:0000\n".encode()


def test_actual_pts_never_substituted_with_requested_time(tmp_path):
    image = tmp_path / "sample.png"
    image.write_bytes(png_header())
    record = probe.inspect_frame(image, showinfo() + showinfo(3.5, 57344), 2.6, 10.4)
    assert record["requested_seconds"] == 2.6
    assert record["actual_pts_seconds"] == 3
    assert record["actual_pts"] == 49152
    assert record["png_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()


@pytest.mark.parametrize("log,reason", [(b"no timestamp", "actual_frame_pts_missing"),
                                        (showinfo(2), "actual_frame_pts_out_of_range"),
                                        (showinfo(100), "actual_frame_pts_out_of_range")])
def test_missing_or_invalid_actual_pts(tmp_path, log, reason):
    image = tmp_path / "sample.png"
    image.write_bytes(png_header())
    with pytest.raises(probe.ProbeStop, match=reason):
        probe.inspect_frame(image, log, 2.6, 10.4)


def test_png_dimensions_are_checked(tmp_path):
    image = tmp_path / "sample.png"
    image.write_bytes(png_header(1280, 720))
    with pytest.raises(probe.ProbeStop, match="preview_dimensions_invalid"):
        probe.inspect_frame(image, showinfo(width=1280, height=720), 2.6, 10.4)


def test_command_is_local_only_and_does_not_upscale():
    command = probe.frame_command(Path("ffmpeg.exe"), Path("local.mp4"), 2.6, Path("new.png"))
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
    assert command[command.index("-f") + 1] == "mov"
    assert "-copyts" in command and "-n" in command
    assert "min(640,iw)" in command[command.index("-vf") + 1]
    assert "min(640,ih)" in command[command.index("-vf") + 1]


def test_duplicate_sampling_kept_but_display_deduplicated(source_fixture, monkeypatch):
    report, _, artifacts = source_fixture
    digest, _ = probe.read_report(report)
    output = probe.output_path(artifacts / "local-preview-dedup", create=True)
    fake_ffmpeg = artifacts / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"SYNTHETIC")
    def extract(command, timeout):
        assert 0 < timeout <= 60
        Path(command[-1]).write_bytes(png_header())
        requested = float(command[command.index("-ss") + 1])
        return showinfo(math_ceil_half(requested), int(math_ceil_half(requested) * 16384))
    monkeypatch.setattr(probe, "bounded_ffmpeg", extract)
    before = report.read_bytes()
    result = probe.run_video(report, digest, 0, output, fake_ffmpeg)
    assert result["preview"] == "PASS"
    assert len(result["samples"]) == 2 and result["display_indices"] == [1]
    assert result["samples"][1]["duplicate_of"] == 1
    assert len(list(output.rglob("*.png"))) == 2
    assert report.read_bytes() == before
    result["source"] = probe.read_report(report)[1][0]
    probe.write_html({"assets": [result]}, output)
    assert (output / "index.html").read_text(encoding="utf-8").count('<img ') == 1


def math_ceil_half(value):
    import math
    return math.ceil(value * 2) / 2


def test_frame_failure_does_not_change_download_report(source_fixture, monkeypatch):
    report, _, artifacts = source_fixture
    before = report.read_bytes()
    digest, _ = probe.read_report(report)
    output = probe.output_path(artifacts / "local-preview-fail", create=True)
    fake_ffmpeg = artifacts / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"SYNTHETIC")
    def fail(*args):
        raise probe.ProbeStop("frame_extract_failed")
    monkeypatch.setattr(probe, "bounded_ffmpeg", fail)
    result = probe.run_video(report, digest, 0, output, fake_ffmpeg)
    assert result["preview"] == "BLOCKED" and result["download"] == "PASS_UNCHANGED"
    assert report.read_bytes() == before


def test_invalid_source_prevents_decoder_start_and_preserves_failure(source_fixture, monkeypatch, capsys):
    report, assets, artifacts = source_fixture
    (report.parent / assets[0]["relative_file"]).unlink()
    fake_ffmpeg = artifacts / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"SYNTHETIC")
    monkeypatch.setattr(probe, "run_bounded", lambda *args: pytest.fail("Unverified source reached decoder"))
    output = artifacts / "local-preview-missing"
    assert probe.main(["--source-report", str(report), "--output", str(output), "--ffmpeg", str(fake_ffmpeg)]) == 2
    result = json.loads((output / "report.json").read_text())
    assert result["stop_reason"] == "source_file_missing" and result["request_count"] == 0
    assert (output / "index.html").exists()
    capsys.readouterr()


def test_worker_handshake_precedes_source_read(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("WRONG\n"))
    monkeypatch.setattr(probe, "run_video", lambda *args: pytest.fail("Worker ran before handshake"))
    assert probe.main(["--worker", "--source-report", "report.json", "--output", "new", "--ffmpeg", "ffmpeg.exe"]) == 2


def test_timeout_closes_private_job_before_cleanup(monkeypatch):
    events = []
    class Job:
        def assign(self, process):
            events.append("assigned")
        def close(self):
            events.append("closed")
    class Process:
        returncode = None
        def communicate(self, input=None, timeout=None):
            if input:
                assert events == ["assigned"] and input == "START_LOCAL_PREVIEW\n" and timeout == 60
                events.append("started")
                raise subprocess.TimeoutExpired("synthetic", timeout)
            assert events[-1] == "closed"
            self.returncode = -1
            return "", None
        def poll(self):
            return self.returncode
    monkeypatch.setattr(probe, "WindowsJob", Job)
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: Process())
    result = probe.run_bounded(Path("report.json"), "a" * 64, 0, Path("output"), Path("ffmpeg.exe"))
    assert result["stop_reason"] == "worker_total_timeout"
    assert result["unverified_partial_files_preserved"]


def test_real_subprocess_timeout_and_log_limit():
    with pytest.raises(probe.ProbeStop, match="video_timeout"):
        probe.bounded_ffmpeg([sys.executable, "-c", "import time; time.sleep(30)"], 0.1)
    with pytest.raises(probe.ProbeStop, match="ffmpeg_log_limit"):
        probe.bounded_ffmpeg([sys.executable, "-c", "import sys; sys.stderr.write('X'*100000); sys.stderr.flush()"], 5)


def test_cancel_closes_job_before_process_cleanup(monkeypatch):
    events = []
    class Job:
        def assign(self, process):
            events.append("assigned")
        def close(self):
            events.append("closed")
    class Process:
        def communicate(self, **kwargs):
            assert events == ["assigned"] and kwargs["input"] == "START_LOCAL_PREVIEW\n"
            raise KeyboardInterrupt
        def poll(self):
            return None
        def kill(self):
            assert events[-1] == "closed"
            events.append("killed")
        def wait(self, timeout):
            events.append("waited")
    monkeypatch.setattr(probe, "WindowsJob", Job)
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(KeyboardInterrupt):
        probe.run_bounded(Path("report.json"), "a" * 64, 0, Path("output"), Path("ffmpeg.exe"))
    assert events == ["assigned", "closed", "killed", "waited"]


def test_cancel_preserves_report_and_does_not_dispatch_next_asset(source_fixture, monkeypatch, capsys):
    source_report, _, artifacts = source_fixture
    before = source_report.read_bytes()
    fake_ffmpeg = artifacts / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"SYNTHETIC")
    calls = []
    def cancel(*args):
        calls.append(args[2])
        raise KeyboardInterrupt
    monkeypatch.setattr(probe, "run_bounded", cancel)
    output = artifacts / "local-preview-canceled"
    assert probe.main(["--source-report", str(source_report), "--output", str(output), "--ffmpeg", str(fake_ffmpeg)]) == 2
    result = json.loads((output / "report.json").read_text())
    assert result["stop_reason"] == "canceled" and result["preview"] == "BLOCKED"
    assert calls == [0] and source_report.read_bytes() == before
    assert "canceled" in (output / "index.html").read_text(encoding="utf-8")
    capsys.readouterr()


@pytest.mark.parametrize("size", ["320x180", "1280x720", "720x1280"])
def test_real_ffmpeg_synthetic_pts_scaling_and_duplicates(source_fixture, size):
    configured = os.environ.get("BILI_TEST_FFMPEG")
    if not configured or not Path(configured).is_file():
        pytest.skip("Set BILI_TEST_FFMPEG to an explicitly selected local ffmpeg.exe")
    report, assets, artifacts = source_fixture
    synthetic = artifacts / "synthetic.mp4"
    subprocess.run([configured, "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
                    f"color=c=blue:s={size}:r=2:d=10.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(synthetic)],
                   check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    content = synthetic.read_bytes()
    for asset in assets:
        (report.parent / asset["relative_file"]).write_bytes(content)
        asset.update(size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    report.write_text(json.dumps({"experiment": "U02_VIDEO_DOWNLOAD", "download": "PASS", "assets": assets}))
    digest, _ = probe.read_report(report)
    output = probe.output_path(artifacts / "local-preview-real-synthetic", create=True)
    before = report.read_bytes()
    result = probe.run_video(report, digest, 0, output, Path(configured))
    assert result["preview"] == "PASS", result
    assert [frame["actual_pts_seconds"] for frame in result["samples"]] == [3, 8]
    assert result["samples"][0]["requested_seconds"] == 2.6
    expected = {"320x180": (320, 180), "1280x720": (640, 360), "720x1280": (360, 640)}[size]
    assert [(frame["width"], frame["height"]) for frame in result["samples"]] == [expected, expected]
    assert result["display_indices"] == [1]
    assert report.read_bytes() == before
