"""Offline preview spike for the three verified U02 assets; never changes downloads."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import threading
import time

_spec = importlib.util.spec_from_file_location("_preview_download", Path(__file__).with_name("video_download_probe.py"))
download = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(download)
ProbeStop, WindowsJob = download.ProbeStop, download.WindowsJob
ROOT = Path(__file__).resolve().parents[1]
FIXED_ASSETS = (("BV13x41117TL", 1, 14694589), ("BV1bK411W797", 1, 191557669),
                ("BV1bK411W797", 2, 191559297))
VIDEO_TIMEOUT = 60
MAX_JSON_BYTES = 1024 * 1024
MAX_LOG_BYTES = 64 * 1024
MAX_PNG_BYTES = 2 * 1024 * 1024


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def no_links(path):
    """Reject Windows junction/reparse points as well as ordinary symlinks."""
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if part.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ProbeStop("linked_path_not_allowed")


def artifacts_path(path):
    path = Path(os.path.abspath(path))
    no_links(path)
    base = (ROOT / ".artifacts").resolve()
    if not base.is_relative_to(ROOT.resolve()) or not path.resolve().is_relative_to(base):
        raise ProbeStop("path_outside_artifacts")
    return path.resolve()


def report_path(path):
    resolved = artifacts_path(path)
    if (resolved.name != "report.json" or resolved.parent.parent != (ROOT / ".artifacts").resolve()
            or not re.fullmatch(r"video-download-[A-Za-z0-9_-]{1,64}", resolved.parent.name)):
        raise ProbeStop("invalid_source_report_path")
    return resolved


def output_path(path, *, create=False):
    resolved = artifacts_path(path)
    if (resolved.parent != (ROOT / ".artifacts").resolve()
            or not re.fullmatch(r"local-preview-[A-Za-z0-9_-]{1,64}", resolved.name)):
        raise ProbeStop("invalid_output_directory")
    if create:
        resolved.mkdir(exist_ok=False)
    elif not resolved.is_dir():
        raise ProbeStop("output_directory_missing")
    return resolved


def read_report(path):
    path = report_path(path)
    try:
        if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
            raise ProbeStop("source_report_missing_or_too_large")
        with path.open("rb") as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ProbeStop("source_report_missing_or_too_large")
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("experiment") != "U02_VIDEO_DOWNLOAD"
                or data.get("download") != "PASS" or len(data.get("assets", [])) != 3):
            raise ValueError
        indexed = {}
        for asset in data["assets"]:
            if not isinstance(asset, dict) or type(asset.get("part")) is not int or type(asset.get("cid")) is not int:
                raise ValueError
            identity = asset.get("bvid"), asset["part"], asset["cid"]
            if identity not in FIXED_ASSETS or identity in indexed:
                raise ValueError
            bvid, part, cid = identity
            expected = f"media/{bvid}/{cid}/asset.mp4"
            if (asset.get("relative_file") != expected
                    or any(asset.get(field) != "PASS" for field in ("download", "verification", "full_decode"))
                    or type(asset.get("size_bytes")) is not int or not 0 < asset["size_bytes"] <= download.MAX_MEDIA_BYTES
                    or not isinstance(asset.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"])):
                raise ValueError
            duration = asset.get("duration_seconds")
            if type(duration) not in (float, int) or not math.isfinite(duration) or not 0 < duration <= 86400:
                raise ValueError
            indexed[identity] = {"bvid": bvid, "part": part, "cid": cid, "relative_file": expected,
                                 "size_bytes": asset["size_bytes"], "sha256": asset["sha256"],
                                 "duration_seconds": duration}
        if set(indexed) != set(FIXED_ASSETS):
            raise ValueError
        return hashlib.sha256(raw).hexdigest(), [indexed[key] for key in FIXED_ASSETS]
    except (ValueError, TypeError, KeyError):
        raise ProbeStop("source_report_contract_invalid") from None
    except OSError:
        raise ProbeStop("source_report_unreadable") from None


def verify_source(source_report, asset):
    path = artifacts_path(report_path(source_report).parent / asset["relative_file"])
    if path.parent.parent.parent.parent != report_path(source_report).parent:
        raise ProbeStop("source_path_outside_report_directory")
    if not path.is_file():
        raise ProbeStop("source_file_missing")
    if path.stat().st_size != asset["size_bytes"]:
        raise ProbeStop("source_size_mismatch")
    if sha256_file(path) != asset["sha256"]:
        raise ProbeStop("source_sha256_mismatch")
    return path


def sample_times(duration):
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ProbeStop("invalid_duration")
    count = min(6, max(1, math.floor(duration / 5)))
    return [duration * (index + 0.5) / count for index in range(count)]


def frame_command(ffmpeg, source, target, destination):
    # -copyts preserves source PTS; showinfo records the selected frame rather
    # than substituting the requested seek time. Only the local MOV demuxer runs.
    return [str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-v", "info", "-n",
            "-ss", f"{target:.9f}", "-copyts", "-protocol_whitelist", "file,pipe", "-f", "mov",
            "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf",
            "scale=w='min(640,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease,setsar=1,showinfo",
            "-frames:v", "1", "-fps_mode", "passthrough", "-map_metadata", "-1", "-threads", "1",
            "-c:v", "png", "-f", "image2", "-update", "1", str(destination)]


def bounded_ffmpeg(command, timeout):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    captured = bytearray()
    overflow = threading.Event()
    def read_log():
        while block := process.stderr.read(4096):
            if len(captured) + len(block) > MAX_LOG_BYTES:
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
                break
            captured.extend(block)
    reader = threading.Thread(target=read_log, daemon=True)
    reader.start()
    try:
        try:
            process.wait(timeout=max(0.01, timeout))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise ProbeStop("video_timeout") from None
        reader.join(timeout=5)
        if reader.is_alive():
            raise ProbeStop("ffmpeg_log_reader_timeout")
        if overflow.is_set():
            raise ProbeStop("ffmpeg_log_limit")
        if process.returncode != 0:
            raise ProbeStop("frame_extract_failed", {"exit_code": process.returncode})
        return bytes(captured)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


def inspect_frame(path, log, requested, duration):
    # The first showinfo frame is the one encoded by -frames:v 1. FFmpeg can
    # report an additional buffered frame; do not mistake it for the PNG frame.
    match = re.search(rb"\[Parsed_showinfo_[^\]]+\].*?\bn:\s*0\s+pts:\s*(-?\d+)\s+pts_time:([-+0-9.eE]+).*?\bs:(\d+)x(\d+)", log)
    if not match:
        raise ProbeStop("actual_frame_pts_missing")
    pts, actual = int(match[1]), float(match[2])
    if not math.isfinite(actual) or actual < requested - 0.0001 or actual > duration + 1:
        raise ProbeStop("actual_frame_pts_out_of_range")
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_PNG_BYTES:
        raise ProbeStop("preview_png_missing_or_too_large")
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ProbeStop("preview_png_invalid")
    width, height = struct.unpack(">II", header[16:24])
    if not 0 < width <= 640 or not 0 < height <= 640 or (width, height) != (int(match[3]), int(match[4])):
        raise ProbeStop("preview_dimensions_invalid")
    return {"requested_seconds": round(requested, 9), "actual_pts": pts, "actual_pts_seconds": actual,
            "width": width, "height": height, "png_sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def run_video(source_report, report_sha256, index, output, ffmpeg):
    started = time.monotonic()
    result = {"preview": "BLOCKED", "samples": [], "display_indices": []}
    try:
        output = output_path(output)
        current_sha, assets = read_report(source_report)
        if current_sha != report_sha256:
            raise ProbeStop("source_report_changed")
        if type(index) is not int or not 0 <= index < len(assets):
            raise ProbeStop("invalid_asset_index")
        asset = assets[index]
        result.update(source=asset, download="PASS_UNCHANGED")
        source = verify_source(source_report, asset)
        ffmpeg, _ = download.check_ffmpeg(ffmpeg)
        directory = output / asset["bvid"] / str(asset["cid"])
        directory.mkdir(parents=True, exist_ok=False)
        known = {}
        for ordinal, target in enumerate(sample_times(asset["duration_seconds"]), 1):
            if time.monotonic() - started >= VIDEO_TIMEOUT:
                raise ProbeStop("video_timeout")
            destination = directory / f"sample-{ordinal:02}.png"
            log = bounded_ffmpeg(frame_command(ffmpeg, source, target, destination),
                                 VIDEO_TIMEOUT - (time.monotonic() - started))
            frame = inspect_frame(destination, log, target, asset["duration_seconds"])
            frame.update(index=ordinal, relative_file=destination.relative_to(output).as_posix())
            duplicate = known.get(frame["png_sha256"])
            frame["duplicate_of"] = duplicate
            if duplicate is None:
                known[frame["png_sha256"]] = ordinal
                result["display_indices"].append(ordinal)
            result["samples"].append(frame)
        verify_source(source_report, asset)
        result["preview"] = "PASS"
    except ProbeStop as exc:
        result.update(preview="PARTIAL" if result["samples"] else "BLOCKED", stop_reason=exc.reason,
                      stop_evidence=exc.evidence)
    except Exception as exc:
        result.update(preview="PARTIAL" if result["samples"] else "BLOCKED", stop_reason="preview_worker_failed",
                      stop_evidence={"exception_type": type(exc).__name__})
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def run_bounded(source_report, report_sha256, index, output, ffmpeg):
    job, process = WindowsJob(), None
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--source-report", str(source_report),
                   "--report-sha256", report_sha256, "--asset-index", str(index), "--output", str(output),
                   "--ffmpeg", str(ffmpeg)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(process)
        try:
            stdout, _ = process.communicate(input="START_LOCAL_PREVIEW\n", timeout=VIDEO_TIMEOUT)
        except subprocess.TimeoutExpired:
            job.close()
            process.communicate(timeout=5)
            return {"preview": "BLOCKED", "stop_reason": "worker_total_timeout", "samples": [],
                    "display_indices": [], "unverified_partial_files_preserved": True}
        if process.returncode != 0 or len(stdout) > MAX_JSON_BYTES:
            raise ProbeStop("preview_worker_failed")
        result = json.loads(stdout)
        if not isinstance(result, dict) or not isinstance(result.get("samples"), list) or len(result["samples"]) > 6:
            raise ProbeStop("invalid_worker_result")
        return result
    finally:
        job.close()
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def write_html(report, output):
    sections = []
    for asset in report.get("assets", []):
        source = asset["source"]
        cards = []
        for frame in asset.get("samples", []):
            if frame.get("duplicate_of") is not None:
                continue
            relative = html.escape(frame["relative_file"], quote=True)
            caption = f"实际 PTS {frame['actual_pts_seconds']:.6f} 秒 · 计划 {frame['requested_seconds']:.6f} 秒"
            cards.append(f'<figure><img src="{relative}" alt="本地视频截图"><figcaption>{caption}</figcaption></figure>')
        state = html.escape(asset.get("stop_reason", asset["preview"]))
        sections.append(f'<section><h2>{source["bvid"]} · P{source["part"]} · CID {source["cid"]}</h2>'
                        f'<p>截图：{state}；下载验证结果保持不变。{len(asset.get("samples", []))} 次采样，'
                        f'{len(asset.get("display_indices", []))} 张去重展示。</p><div class="grid">{"".join(cards)}</div></section>')
    overall = html.escape(report.get("stop_reason", report.get("preview", "NOT_TESTED")))
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\'; style-src \'unsafe-inline\'">'
            '<title>本地视频预览研究</title><style>body{font:16px sans-serif;background:#faf7f9;color:#25232a;margin:32px}'
            'h1{color:#d44b83}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}'
            'figure{margin:0;padding:12px;background:white;border-radius:12px}img{display:block;max-width:100%;height:auto}'
            'figcaption{font-size:13px;margin-top:8px}section{margin:32px 0}</style>'
            '<h1>本地视频预览研究</h1><p>仅展示已核验的本地文件。时间来自 FFmpeg showinfo；相同 PNG SHA-256 仅展示一次，'
            '全部采样记录保留在 report.json。本页不代表产品或播放器验收。</p>'
            + f'<p>本轮预览结果：{overall}</p>' + ''.join(sections) + '</html>')
    (output / "index.html").write_text(page, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--asset-index", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--report-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if sys.stdin.readline() != "START_LOCAL_PREVIEW\n":
            return 2
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = run_video(args.source_report, args.report_sha256, args.asset_index, args.output, args.ffmpeg)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    try:
        source_report = report_path(args.source_report)
        ffmpeg, _ = download.check_ffmpeg(args.ffmpeg)
        output = output_path(args.output, create=True)
    except (OSError, ProbeStop):
        parser.error("Use an existing .artifacts/video-download-NAME/report.json, a new .artifacts/local-preview-NAME, and ffmpeg.exe")
    report = {"schema_version": 1, "experiment": "U04_LOCAL_PREVIEW", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "research_gate": "NEEDS_MORE_EVIDENCE", "preview": "IN_PROGRESS", "assets": [], "request_count": 0,
              "max_samples_per_video": 6, "worker_timeout_seconds": VIDEO_TIMEOUT, "download_report_modified": False,
              "media_uploaded": False, "playback": "NOT_TESTED", "deduplication": "identical_png_sha256"}
    with (output / "report.json").open("x", encoding="utf-8") as handle:
        def save():
            handle.seek(0)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.truncate()
            handle.flush()
        save()
        try:
            report_sha, assets = read_report(source_report)
            report["source_report_sha256"] = report_sha
            report["ffmpeg_sha256"] = sha256_file(ffmpeg)
            # Verify every source before the first decoder process is allowed.
            for asset in assets:
                verify_source(source_report, asset)
            for index, asset in enumerate(assets):
                result = run_bounded(source_report, report_sha, index, output, ffmpeg)
                result["source"] = asset
                result["download"] = "PASS_UNCHANGED"
                report["assets"].append(result)
                save()
            if sha256_file(source_report) != report_sha:
                raise ProbeStop("source_report_changed")
            report["preview"] = "PASS" if all(a["preview"] == "PASS" for a in report["assets"]) else "PARTIAL"
        except KeyboardInterrupt:
            report.update(preview="PARTIAL" if report["assets"] else "BLOCKED", stop_reason="canceled")
        except ProbeStop as exc:
            report.update(preview="PARTIAL" if report["assets"] else "BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
        except Exception as exc:
            report.update(preview="PARTIAL" if report["assets"] else "BLOCKED", stop_reason="preview_parent_failed",
                          stop_evidence={"exception_type": type(exc).__name__})
        save()
    write_html(report, output)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["preview"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
