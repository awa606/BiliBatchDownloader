"""U03: fixed-sample bounded gallery download experiment, never a product downloader."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request

from research_probe import BoundedClient, NoRedirect, ProbeStop, load_bili_cookies, run_account
from gallery_probe import author_identity
import video_download_probe as shared_process

ROOT = Path(__file__).resolve().parents[1]
PINNED_GALLERY = "1.32.16"
SAMPLES = {
    "1249456774436618276": {"images": 4, "image_ext": "png", "live": False},
    "1257912749013336118": {"images": 9, "image_ext": "jpg", "live": True},
}
CDN_HOSTS = frozenset({"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"})
VIDEO_CDN_SUFFIXES = ("bilivideo.com", "bilivideo.cn")
ALLOWED_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "mp4"})
MAX_ASSETS = 20
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PAGE_BYTES = 3 * 1024 * 1024
WORKER_TIMEOUT = 180
START_TOKEN = "START_U03_GALLERY\n"


class WindowsJob(shared_process.WindowsJob):
    """Normalize the shared helper's separately loaded ProbeStop class."""
    def __init__(self):
        try:
            super().__init__()
        except shared_process.ProbeStop as exc:
            raise ProbeStop(exc.reason, exc.evidence) from None

    def assign(self, process):
        try:
            super().assign(process)
        except shared_process.ProbeStop as exc:
            raise ProbeStop(exc.reason, exc.evidence) from None


def check_ffmpeg(path):
    try:
        ffmpeg, ffprobe = shared_process.check_ffmpeg(path)
    except shared_process.ProbeStop as exc:
        raise ProbeStop(exc.reason, exc.evidence) from None
    if ffprobe is None:
        raise ProbeStop("ffprobe_required_for_gallery_verification")
    return ffmpeg, ffprobe


def sample_id(value):
    if value in SAMPLES:
        return value
    match = re.fullmatch(r"https://(?:www\.bilibili\.com/opus|t\.bilibili\.com)/(\d+)", value)
    if match and match[1] in SAMPLES:
        return match[1]
    raise ProbeStop("sample_not_preregistered")


def source_url(dynamic_id):
    return "https://www.bilibili.com/opus/" + sample_id(dynamic_id)


def check_workdir(path, *, create):
    base = (ROOT / ".artifacts").resolve()
    resolved = path.resolve()
    if not base.is_relative_to(ROOT.resolve()) or resolved.parent != base or path.is_symlink():
        raise ProbeStop("workdir_outside_artifacts")
    if not re.fullmatch(r"gallery-download-[A-Za-z0-9_-]{1,64}", resolved.name):
        raise ProbeStop("invalid_workdir_name")
    if create:
        resolved.mkdir(exist_ok=False)
    elif not resolved.is_dir():
        raise ProbeStop("invalid_worker_directory")
    return resolved


def media_extension(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        extension = Path(parsed.path).suffix[1:].lower()
        host = parsed.hostname or ""
        video_host = (extension == "mp4" and re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host)
                      and all(label and len(label) <= 63 and not label.startswith("-") and not label.endswith("-")
                              for label in host.split("."))
                      and any(host.endswith("." + suffix) for suffix in VIDEO_CDN_SUFFIXES))
        if (not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url)
                or parsed.scheme != "https" or not (host in CDN_HOSTS or video_host)
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError
        return extension
    except (ValueError, TypeError, AttributeError):
        raise ProbeStop("media_origin_or_extension_not_allowed") from None


def asset_descriptor(dynamic_id, url, metadata):
    dynamic_id = sample_id(dynamic_id)
    extension = media_extension(url)
    sequence, suffix = metadata.get("num"), metadata.get("suffix", "")
    if type(sequence) is not int or not 1 <= sequence <= MAX_ASSETS or suffix not in ("", "l"):
        raise ProbeStop("invalid_asset_sequence")
    if (suffix == "l") != (extension == "mp4"):
        raise ProbeStop("invalid_livephoto_pair_type")
    return {"sequence": sequence, "pair_role": "live_video" if suffix == "l" else "image",
            "original_extension": extension, "filename": f"{dynamic_id}_{sequence:03d}{suffix}.{extension}",
            "url": url}


def validate_assets(dynamic_id, assets, declared_images):
    contract = SAMPLES[sample_id(dynamic_id)]
    expected = []
    for sequence in range(1, contract["images"] + 1):
        expected.append((sequence, "image", contract["image_ext"]))
        if contract["live"]:
            expected.append((sequence, "live_video", "mp4"))
    actual = [(a["sequence"], a["pair_role"], a["original_extension"]) for a in assets]
    if (type(declared_images) is not int or declared_images != contract["images"]
            or len(assets) > MAX_ASSETS or actual != expected
            or len({a["filename"] for a in assets}) != len(assets)
            or len({a["url"] for a in assets}) != len(assets)):
        raise ProbeStop("fixed_sample_contract_changed")


class Transport:
    """One authenticated source request; credential-free, redirect-free media requests."""
    def __init__(self, jar, dynamic_id, deadline, *, account_requests=1):
        self.page = source_url(dynamic_id)
        self.page_opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
        self.media_opener = urllib.request.build_opener(NoRedirect())
        self.deadline, self.count = deadline, account_requests
        self.page_requests, self.media_requests, self.total_bytes = 0, 0, 0
        self.page_sha256, self.allowed_media = None, set()
        self.last_request_at = 0.0

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ProbeStop("worker_total_timeout")
        return left

    def open(self, url, *, media):
        self.remaining()
        if self.last_request_at:
            delay = max(0.0, 1.0 - (time.monotonic() - self.last_request_at))
            if delay >= self.remaining():
                raise ProbeStop("worker_total_timeout")
            time.sleep(delay)
        if self.count >= 2 + MAX_ASSETS:
            raise ProbeStop("request_budget_exhausted")
        self.count += 1
        self.last_request_at = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.bilibili.com/", "Accept-Encoding": "identity"})
        opener = self.media_opener if media else self.page_opener
        try:
            response = opener.open(request, timeout=min(15.0, self.remaining()))
        except ProbeStop:
            raise
        except urllib.error.HTTPError as exc:
            raise ProbeStop("http_or_redirect_stop", {"http_status": exc.code}) from None
        except Exception as exc:
            raise ProbeStop("transport_stop", {"exception_type": type(exc).__name__}) from None
        if response.status != 200:
            status = response.status
            response.close()
            raise ProbeStop("http_or_redirect_stop", {"http_status": status})
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            response.close()
            raise ProbeStop("unexpected_content_encoding")
        return response

    def fetch_page(self, target, *args, **kwargs):
        if target != self.page or args or kwargs:
            raise ProbeStop("unexpected_gallery_endpoint")
        if self.page_requests:
            raise ProbeStop("source_request_budget_exhausted")
        self.page_requests += 1
        with self.open(target, media=False) as response:
            body = response.read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise ProbeStop("page_too_large")
        self.page_sha256 = hashlib.sha256(body).hexdigest()
        if b"window._riskdata_" in body:
            raise ProbeStop("verification_page_detected")
        return SimpleNamespace(text=body.decode("utf-8", errors="replace"))

    def allow_assets(self, assets):
        if len(assets) > MAX_ASSETS:
            raise ProbeStop("asset_budget_exhausted")
        for asset in assets:
            media_extension(asset["url"])
        self.allowed_media = {asset["url"] for asset in assets}

    def download(self, asset, destination):
        url = asset["url"]
        media_extension(url)
        if url not in self.allowed_media or self.media_requests >= MAX_ASSETS:
            raise ProbeStop("media_not_preregistered_or_budget_exhausted")
        self.allowed_media.remove(url)  # No retry of this asset in the same experiment.
        self.media_requests += 1
        size, digest = 0, hashlib.sha256()
        with self.open(url, media=True) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                if not re.fullmatch(r"[0-9]{1,12}", declared):
                    raise ProbeStop("invalid_content_length")
                declared = int(declared)
                if declared <= 0 or declared > MAX_ASSET_BYTES or declared > MAX_TOTAL_BYTES - self.total_bytes:
                    raise ProbeStop("media_byte_budget_exhausted")
            with destination.open("xb") as handle:
                while True:
                    self.remaining()
                    limit = min(MAX_ASSET_BYTES - size, MAX_TOTAL_BYTES - self.total_bytes)
                    if declared is not None and size == declared:
                        break
                    if limit <= 0:
                        raise ProbeStop("media_byte_budget_exhausted")
                    block = response.read(min(128 * 1024, limit))
                    if not block:
                        break
                    if len(block) > limit:
                        raise ProbeStop("media_byte_budget_exhausted")
                    handle.write(block)
                    digest.update(block)
                    size += len(block)
                    self.total_bytes += len(block)
        if size <= 0 or declared is not None and size != declared:
            raise ProbeStop("media_length_mismatch")
        return {"size_bytes": size, "sha256": digest.hexdigest()}


def discover_assets(dynamic_id, transport):
    if importlib.metadata.version("gallery-dl") != PINNED_GALLERY:
        raise ProbeStop("gallery_version_mismatch")
    from gallery_dl import config, extractor
    config.set(("extractor",), "retries", 0)
    config.set(("extractor",), "timeout", 15)
    config.set(("extractor", "bilibili"), "livephoto", True)
    item = extractor.find(source_url(dynamic_id))
    if item is None or type(item).__name__ != "BilibiliArticleExtractor" or item.groups[0] != dynamic_id:
        raise ProbeStop("unexpected_gallery_extractor")
    item.request = transport.fetch_page
    def no_upstream_wait(*args, **kwargs):
        raise ProbeStop("upstream_wait_blocked")
    item.wait = no_upstream_wait
    item.initialize()
    assets, identity, declared_images = [], None, None
    for message in item:
        if message[0] == 2:
            if identity is not None:
                raise ProbeStop("multiple_directory_messages")
            identity = author_identity(message[2].get("user_id"))
            declared_images = message[2].get("count")
        elif message[0] == 3:
            assets.append(asset_descriptor(dynamic_id, message[1], message[2]))
            if len(assets) > MAX_ASSETS:
                raise ProbeStop("asset_budget_exhausted")
        else:
            raise ProbeStop("unexpected_gallery_message")
    if identity is None or transport.page_requests != 1:
        raise ProbeStop("missing_gallery_contract")
    validate_assets(dynamic_id, assets, declared_images)
    return assets, identity


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
        decoded = subprocess.run([str(ffmpeg), "-nostdin", *common, "-xerror", "-err_detect", "explode", "-i", str(path),
                    "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=timeout(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if decoded.returncode != 0:
            raise ProbeStop("local_decode_failed")
        evidence = {"decode": "PASS", "streams": safe_streams, "demuxer": demuxer,
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


def run_worker(profile, dynamic_id, workdir, ffmpeg):
    report = {"download": "NOT_TESTED", "assets": [], "network_requests": 0,
              "research_gate": "NEEDS_MORE_EVIDENCE", "playback": "NOT_TESTED", "preview": "NOT_TESTED"}
    client = transport = None
    deadline = time.monotonic() + WORKER_TIMEOUT
    logging.disable(logging.CRITICAL)
    try:
        dynamic_id = sample_id(dynamic_id)
        workdir = check_workdir(workdir, create=False)
        ffmpeg, ffprobe = check_ffmpeg(ffmpeg)
        jar, _ = load_bili_cookies(profile, None)
        client = BoundedClient(jar, max_requests=1)
        account = run_account(client.get)
        if account.get("account") != "PASS":
            raise ProbeStop(account.get("stop_reason", "account_not_validated"), account.get("stop_evidence", {}))
        transport = Transport(jar, dynamic_id, deadline, account_requests=client.request_count)
        assets, identity = discover_assets(dynamic_id, transport)
        transport.allow_assets(assets)
        report.update(author_uid=identity, sample_dynamic_id=dynamic_id, declared_asset_count=len(assets))
        for asset in assets:
            evidence = {key: value for key, value in asset.items() if key != "url"}
            evidence["download"] = "IN_PROGRESS"
            report["assets"].append(evidence)
            path = workdir / asset["filename"]
            evidence.update(transport.download(asset, path))
            evidence["download"] = "COMPLETE_NOT_DECODED"
            evidence.update(inspect_asset(path, asset["original_extension"], ffmpeg, ffprobe, deadline))
            evidence["download"] = "PASS"
        report["download"] = "PASS"
    except ProbeStop as exc:
        report.update(download="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence, partial_files_preserved=True)
    except Exception as exc:
        report.update(download="BLOCKED", stop_reason="unexpected_gallery_probe_error",
                      stop_evidence={"exception_type": type(exc).__name__}, partial_files_preserved=True)
    finally:
        report["network_requests"] = transport.count if transport else client.request_count if client else 0
        if transport:
            report.update(source_response_sha256=transport.page_sha256, source_requests=transport.page_requests,
                          media_requests=transport.media_requests, total_media_bytes=transport.total_bytes)
    return report


def run_bounded(profile, dynamic_id, workdir, ffmpeg):
    job, process = WindowsJob(), None
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--profile", profile,
                   "--sample", dynamic_id, "--workdir", str(workdir), "--ffmpeg", str(ffmpeg)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        job.assign(process)
        try:
            stdout, _ = process.communicate(input=START_TOKEN, timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            job.close()
            process.communicate(timeout=5)
            return {"download": "BLOCKED", "stop_reason": "worker_total_timeout", "partial_files_preserved": True}
        if process.returncode != 0 or len(stdout) > 256 * 1024:
            raise ProbeStop("worker_failed")
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ProbeStop("invalid_worker_result")
        return result
    finally:
        job.close()
        if process and process.poll() is None:
            process.kill()  # Assignment failed before START_TOKEN, so no child work began.
            process.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--sample", required=True, help="One of the two preregistered dynamic IDs or exact URLs")
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if sys.stdin.readline() != START_TOKEN:
            return 2
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = run_worker(args.profile, args.sample, args.workdir, args.ffmpeg)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    try:
        dynamic_id = sample_id(args.sample)
        ffmpeg, _ = check_ffmpeg(args.ffmpeg)
        workdir = check_workdir(args.workdir, create=True)
    except ProbeStop as exc:
        parser.error(exc.reason)
    except OSError:
        parser.error("Use one fixed sample, a new .artifacts/gallery-download-NAME directory, and existing FFmpeg/ffprobe")
    report = {"schema_version": 1, "experiment": "U03_GALLERY_DOWNLOAD", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "sample_dynamic_id": dynamic_id, "gallery_dl_required": PINNED_GALLERY, "download": "IN_PROGRESS",
              "research_gate": "NEEDS_MORE_EVIDENCE", "max_assets": MAX_ASSETS, "max_asset_bytes": MAX_ASSET_BYTES,
              "max_total_bytes": MAX_TOTAL_BYTES, "worker_timeout_seconds": WORKER_TIMEOUT,
              "credentials_persisted": False, "source_urls_persisted": False, "raw_stderr_persisted": False,
              "preview": "NOT_TESTED", "playback": "NOT_TESTED", "assets": []}
    with (workdir / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle)
        handle.flush()
        try:
            report.update(run_bounded(args.profile, dynamic_id, workdir, ffmpeg))
        except ProbeStop as exc:
            report.update(download="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
        except Exception as exc:
            report.update(download="BLOCKED", stop_reason="parent_probe_failed", stop_evidence={"exception_type": type(exc).__name__})
        handle.seek(0)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.truncate()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    expected = SAMPLES[dynamic_id]["images"] * (2 if SAMPLES[dynamic_id]["live"] else 1)
    return 0 if report.get("download") == "PASS" and len(report.get("assets", [])) == expected else 2


if __name__ == "__main__":
    raise SystemExit(main())
