"""U02: fixed, bounded download experiment. Never use as a production downloader."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

_spec = importlib.util.spec_from_file_location("_download_video_probe", Path(__file__).with_name("video_probe.py"))
video = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(video)
common, ProbeStop = video.common, video.ProbeStop
ROOT = Path(__file__).resolve().parents[1]
WORKER_TIMEOUT = 230
REQUEST_LIMIT = 36
MAX_MEDIA_BYTES = 150 * 1024 * 1024
MAX_METADATA_BYTES = 3 * 1024 * 1024
SAMPLES = (("BV13x41117TL", 1), ("BV1bK411W797", 1), ("BV1bK411W797", 2))
API_PATHS = {"/x/web-interface/nav", "/x/player/pagelist", "/x/player/wbi/playurl",
             "/x/player/wbi/v2", "/x/web-interface/wbi/view/detail", "/x/web-interface/view"}
CDN_SUFFIXES = ("bilivideo.com", "bilivideo.cn", "hdslb.com")


def merger_postprocessor_args():
    # Pinned yt-dlp real_run_ffmpeg checks merger+ffmpeg_i1 then
    # merger+ffmpeg_i separately for each input. No network-capable protocols.
    return {"merger+ffmpeg_i": ["-protocol_whitelist", "file,pipe", "-f", "mov"]}


def sample_url(bvid, part):
    return f"https://www.bilibili.com/video/{bvid}" + (f"?p={part}" if bvid == "BV1bK411W797" else "")


def check_workdir(path, *, create):
    base = (ROOT / ".artifacts").resolve()
    resolved = path.resolve()
    if not base.is_relative_to(ROOT.resolve()) or resolved.parent != base:
        raise ProbeStop("workdir_outside_artifacts")
    if not re.fullmatch(r"video-download-[A-Za-z0-9_-]{1,64}", resolved.name):
        raise ProbeStop("invalid_workdir_name")
    if create:
        resolved.mkdir(exist_ok=False)
    elif not resolved.is_dir() or resolved.is_symlink():
        raise ProbeStop("invalid_worker_directory")
    return resolved


def check_ffmpeg(path):
    try:
        ffmpeg = path.resolve(strict=True)
        ffprobe = ffmpeg.with_name("ffprobe.exe")
        if ffmpeg.name.lower() != "ffmpeg.exe" or not ffmpeg.is_file():
            raise ValueError
        return ffmpeg, ffprobe if ffprobe.is_file() else None
    except (ValueError, OSError):
        raise ProbeStop("explicit_ffmpeg_required") from None


class WindowsJob:
    """Private Job Object; child must wait for stdin until assignment succeeds."""
    def __init__(self):
        if os.name != "nt":
            raise ProbeStop("windows_job_required")
        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ProbeStop("job_creation_failed")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ProbeStop("job_configuration_failed")

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ProbeStop("job_assignment_failed")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def cdn_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and any(host == d or host.endswith("." + d) for d in CDN_SUFFIXES))


class NoAutomaticRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LimitedReader:
    def __init__(self, response, limit):
        self.response, self.limit, self.count = response, limit, 0
    @property
    def closed(self):
        return self.response.closed
    def readable(self):
        return True
    def read(self, size=None):
        remaining = self.limit - self.count
        data = self.response.read(min(size if size is not None and size >= 0 else remaining + 1, remaining + 1))
        self.count += len(data)
        if self.count > self.limit:
            self.close()
            raise ProbeStop("media_size_limit")
        return data
    def close(self):
        self.response.close()


class BoundedTransport:
    """All yt-dlp HTTP requests, including allowed CDN redirects, pass here."""
    def __init__(self, jar, initial_requests=0):
        self.count, self.redirect_count = initial_requests, 0
        self.media_urls = set()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoAutomaticRedirect(),
                                                 urllib.request.HTTPCookieProcessor(jar))

    def allow_media(self, urls):
        if len(urls) != 2 or not all(cdn_url(url) for url in urls):
            raise ProbeStop("media_origin_not_allowed")
        self.media_urls = set(urls)

    def request(self, request, *, default_headers=None):
        from yt_dlp.networking import Request, Response
        from yt_dlp.utils.networking import HTTPHeaderDict
        if isinstance(request, str):
            request = Request(request)
        if not isinstance(request, Request) or request.method not in ("GET", "HEAD") or request.data is not None:
            raise ProbeStop("request_shape_not_allowed")
        url = request.url
        media = url in self.media_urls
        parsed = urllib.parse.urlsplit(url)
        metadata = (url in {sample_url(*sample) for sample in SAMPLES}
                    or (parsed.scheme == "https" and parsed.netloc == "api.bilibili.com" and parsed.path in API_PATHS))
        if not media and not metadata:
            raise ProbeStop("request_origin_or_endpoint_not_allowed")
        # Cookies are added afresh by CookieJar for their own domains. Never carry
        # a raw Cookie or Authorization header across a media redirect.
        headers = {key: val for key, val in HTTPHeaderDict(default_headers, request.headers).items()
                   if key.lower() not in {"cookie", "authorization", "proxy-authorization", "accept-encoding"}}
        headers["Accept-Encoding"] = "identity"
        for hop in range(3):
            if self.count >= REQUEST_LIMIT:
                raise ProbeStop("request_budget_exhausted")
            self.count += 1
            try:
                response = self.opener.open(urllib.request.Request(url, headers=headers, method=request.method), timeout=15)
            except urllib.error.HTTPError as exc:
                status = exc.code
                location = exc.headers.get("Location")
                exc.close()
                if status in (301, 302, 303, 307, 308) and media and location:
                    next_url = urllib.parse.urljoin(url, location)
                    if hop >= 2 or not cdn_url(next_url):
                        raise ProbeStop("media_redirect_not_allowed") from None
                    self.redirect_count += 1
                    url = next_url
                    continue
                raise ProbeStop("http_or_redirect_stop", {"http_status": status}) from None
            except Exception as exc:
                if isinstance(exc, ProbeStop):
                    raise
                raise ProbeStop("transport_stop", {"exception_type": type(exc).__name__}) from None
            if response.status not in (200, 206) or (not media and response.status != 200):
                status = response.status
                response.close()
                raise ProbeStop("unexpected_http_status", {"http_status": status})
            if media:
                mime = response.headers.get("Content-Type", "").lower()
                if not any(kind in mime for kind in ("video/", "audio/", "application/octet-stream", "application/mp4")):
                    response.close()
                    raise ProbeStop("unexpected_media_content_type")
                return Response(LimitedReader(response, MAX_MEDIA_BYTES), url, response.headers, response.status)
            try:
                body = response.read(MAX_METADATA_BYTES + 1)
            finally:
                response.close()
            if len(body) > MAX_METADATA_BYTES:
                raise ProbeStop("metadata_size_limit")
            if b"window._riskdata_" in body or b"window.__riskdata__" in body:
                raise ProbeStop("platform_verification_page")
            if parsed.netloc == "api.bilibili.com":
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise ProbeStop("api_non_json") from None
                if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
                    code = payload.get("code") if isinstance(payload, dict) and type(payload.get("code")) is int else None
                    raise ProbeStop("api_stop", {"api_code": code})
            return Response(io.BytesIO(body), url, response.headers, response.status)
        raise ProbeStop("media_redirect_not_allowed")


def choose_streams(formats):
    def eligible(fmt):
        if not isinstance(fmt, dict) or not isinstance(fmt.get("url"), str) or not cdn_url(fmt["url"]):
            return False
        # Raw BilibiliBaseIE.extract_formats DASH records omit protocol. Only
        # infer this missing field after the HTTPS CDN URL passed validation.
        protocol = fmt.get("protocol")
        if protocol is None:
            protocol = urllib.parse.urlsplit(fmt["url"]).scheme
        return (protocol in {"http", "https"}
                and not fmt.get("fragments") and isinstance(fmt.get("format_id"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fmt["format_id"]))
    formats = [fmt for fmt in formats if eligible(fmt)]
    videos = [fmt for fmt in formats if 0 < (fmt.get("height") or 0) <= 360 and fmt.get("acodec") == "none"
              and str(fmt.get("vcodec", "")).startswith(("avc1", "h264")) and fmt.get("ext") == "mp4"]
    audios = [fmt for fmt in formats if fmt.get("vcodec") == "none" and str(fmt.get("acodec", "")).startswith(("mp4a", "aac"))
              and fmt.get("ext") in {"m4a", "mp4"}]
    if not videos or not audios:
        raise ProbeStop("required_360p_avc_and_aac_streams_unavailable")
    return max(videos, key=lambda fmt: (fmt["height"], fmt.get("tbr") or 0)), min(audios, key=lambda fmt: fmt.get("tbr") or 0)


def decode_with_ffmpeg(path, ffmpeg):
    # Existing explicitly supplied binary only. The decoder receives a local
    # artifact path, never the remote/signed media URL or cookie headers.
    result = subprocess.run([str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-v", "info", "-xerror",
                             "-err_detect", "explode", "-protocol_whitelist", "file,pipe", "-f", "mov", "-i", str(path),
                             "-map", "0:v:0", "-map", "0:a:0", "-progress", "pipe:1", "-f", "null", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    if result.returncode != 0:
        raise ProbeStop("ffmpeg_full_decode_failed")
    # Raw decoder messages remain only in memory; persist whitelist statistics.
    progress = result.stdout.decode("utf-8", errors="replace")
    messages = result.stderr.decode("utf-8", errors="replace").split("Output #0", 1)[0]
    video_lines = [line for line in messages.splitlines() if re.search(r"Stream #0:.*: Video:", line)]
    audio_lines = [line for line in messages.splitlines() if re.search(r"Stream #0:.*: Audio:", line)]
    video_codecs = [match[1] for line in video_lines if (match := re.search(r": Video: ([A-Za-z0-9_]+)\b", line))]
    audio_codecs = [match[1] for line in audio_lines if (match := re.search(r": Audio: ([A-Za-z0-9_]+)\b", line))]
    if (len(video_codecs) != len(video_lines) or len(audio_codecs) != len(audio_lines)
            or any(codec != "h264" for codec in video_codecs) or any(codec != "aac" for codec in audio_codecs)):
        raise ProbeStop("unexpected_decoded_codec")
    heights = [int(match[2]) for line in video_lines if (match := re.search(r"\b([1-9][0-9]*)x([1-9][0-9]*)\b", line))]
    times = re.findall(r"^out_time_us=([0-9]+)$", progress, re.MULTILINE)
    if not times or not re.search(r"^progress=end\s*$", progress, re.MULTILINE) or len(heights) != len(video_lines):
        raise ProbeStop("ffmpeg_decode_summary_missing")
    return {"streams": [{"codec_type": "video", "codec_name": "h264", "height": height} for height in heights]
                       + [{"codec_type": "audio", "codec_name": "aac"} for _ in audio_lines],
            "format": {"duration": int(times[-1]) / 1_000_000}}


def validate_media_info(info, expected_duration):
    try:
        videos = [item for item in info["streams"] if item.get("codec_type") == "video"]
        audios = [item for item in info["streams"] if item.get("codec_type") == "audio"]
        duration = float(info["format"]["duration"])
        if not videos or not audios or not math.isfinite(duration) or duration <= 0:
            raise ValueError
        if any(item.get("codec_name") != "h264" for item in videos) or any(item.get("codec_name") != "aac" for item in audios):
            raise ValueError
        if any(type(item.get("height")) is not int or not 0 < item["height"] <= 360 for item in videos):
            raise ValueError
        if abs(duration - expected_duration) > max(2.0, expected_duration * 0.025):
            raise ValueError
        return videos, audios, duration
    except (KeyError, TypeError, ValueError):
        raise ProbeStop("media_track_or_duration_verification_failed") from None


def inspect_file(path, ffprobe, expected_duration, *, ffmpeg=None):
    if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_MEDIA_BYTES:
        raise ProbeStop("merged_file_missing_or_size_limit")
    if not ffmpeg:
        raise ProbeStop("local_media_verifier_unavailable")
    if ffprobe:
        result = subprocess.run([str(ffprobe), "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov", "-show_entries",
                                 "stream=codec_type,codec_name,width,height:format=duration,size", "-of", "json", str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        if result.returncode != 0:
            raise ProbeStop("ffprobe_failed")
        try:
            info = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            raise ProbeStop("ffprobe_summary_invalid") from None
        validate_media_info(info, expected_duration)
        method = "ffprobe_and_full_decode"
    else:
        method = "ffmpeg_full_decode"
    # Container headers alone cannot prove that compressed frames are intact.
    decoded = decode_with_ffmpeg(path, ffmpeg)
    videos, audios, duration = validate_media_info(decoded, expected_duration)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"video_tracks": len(videos), "audio_tracks": len(audios), "height": max(item["height"] for item in videos),
            "duration_seconds": round(duration, 3), "size_bytes": path.stat().st_size, "sha256": digest.hexdigest(),
            "preview": "NOT_TESTED", "playback": "NOT_TESTED", "verification": "PASS",
            "video_codec": "h264", "audio_codec": "aac", "full_decode": "PASS",
            "verification_method": method, "ffprobe": "PASS" if ffprobe else "NOT_AVAILABLE"}


def download_one(sample, jar, transport, workdir, ffmpeg, ffprobe):
    from yt_dlp import YoutubeDL
    from yt_dlp.cookies import YoutubeDLCookieJar
    from yt_dlp.extractor.bilibili import BiliBiliIE
    bvid, part = sample
    observed = {}
    class ResearchIE(BiliBiliIE):
        @classmethod
        def ie_key(cls):
            return BiliBiliIE.ie_key()
        def _search_json(self, *args, **kwargs):
            result = super()._search_json(*args, **kwargs)
            video.capture_pages(result, observed)
            return result
        def _download_json(self, *args, **kwargs):
            result = super()._download_json(*args, **kwargs)
            if args and "/x/player/pagelist" in str(args[0]):
                video.capture_pages(result, observed)
            return result
    class ResearchYDL(YoutubeDL):
        def urlopen(self, request):
            return transport.request(request, default_headers=self.params["http_headers"])
    options = {"logger": video.StopLogger(), "quiet": True, "no_warnings": False, "noplaylist": True,
               "retries": 0, "fragment_retries": 0, "extractor_retries": 0, "file_access_retries": 0,
               "socket_timeout": 15, "cachedir": False, "writeinfojson": False, "writethumbnail": False,
               "writesubtitles": False, "writeautomaticsub": False, "getcomments": False,
               "overwrites": False, "continuedl": False, "nopart": False, "fixup": "never",
               "ffmpeg_location": str(ffmpeg), "merge_output_format": "mp4", "concurrent_fragment_downloads": 1,
               "postprocessor_args": merger_postprocessor_args(),
               "max_filesize": MAX_MEDIA_BYTES, "http_chunk_size": 0, "hls_prefer_native": True,
               "outtmpl": str(workdir / "unused.%(ext)s")}
    with ResearchYDL(options, auto_init=False) as ydl:
        memory_jar = YoutubeDLCookieJar()
        for cookie in jar:
            memory_jar.set_cookie(cookie)
        ydl.cookiejar = memory_jar
        ydl.add_info_extractor(ResearchIE())
        info = ydl.extract_info(sample_url(*sample), download=False, process=False)
        if not isinstance(info, dict) or info.get("_type", "video") != "video" or info.get("id") not in {bvid, f"{bvid}_p{part}"}:
            raise ProbeStop("unexpected_sample_identity")
        page = next((entry for entry in observed.get("pages", []) if entry["page"] == part), None)
        uid = video.positive_int(info.get("uploader_id"))
        duration = info.get("duration")
        if not page or not uid or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ProbeStop("sample_identity_or_duration_missing")
        visual, audio = choose_streams(info.get("formats") or [])
        transport.allow_media([visual["url"], audio["url"]])
        target = workdir / "media" / bvid / str(page["cid"])
        target.mkdir(parents=True, exist_ok=False)
        ydl.params["format"] = visual["format_id"] + "+" + audio["format_id"]
        ydl.format_selector = ydl.build_format_selector(ydl.params["format"])
        ydl.params["outtmpl"]["default"] = str(target / "asset.%(ext)s")
        # Restrict process_ie_result to exactly the preselected direct streams.
        # No backup format or fallback URL is supplied.
        info["formats"] = [visual, audio]
        ydl.process_ie_result(info, download=True)
    verified = inspect_file(target / "asset.mp4", ffprobe, duration, ffmpeg=ffmpeg)
    return {"bvid": bvid, "part": part, "cid": page["cid"], "author_uid": str(uid),
            "relative_file": f"media/{bvid}/{page['cid']}/asset.mp4", "download": "PASS", **verified}


def run_worker(profile, workdir, ffmpeg):
    report = {"account": "NOT_TESTED", "download": "NOT_TESTED", "assets": [], "media_attempts": 0}
    transport = None
    try:
        check_workdir(workdir, create=False)
        ffmpeg, ffprobe = check_ffmpeg(ffmpeg)
        if importlib.metadata.version("yt-dlp") != video.PINNED_VERSION:
            raise ProbeStop("dependency_version_mismatch")
        jar, evidence = common.load_bili_cookies(profile, None)
        report["cookie_evidence"] = evidence
        client = common.BoundedClient(jar)
        account = common.run_account(client.get)
        report.update(account)
        report["request_count"] = client.request_count
        if account.get("account") != "PASS" or "stop_reason" in account:
            return report
        transport = BoundedTransport(jar, client.request_count)
        for sample in SAMPLES:
            report["media_attempts"] += 1
            report["assets"].append(download_one(sample, jar, transport, workdir, ffmpeg, ffprobe))
        report["download"] = "PASS"
    except ProbeStop as exc:
        report.update(download="PARTIAL" if report["assets"] else "BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
    except Exception as exc:
        report.update(download="PARTIAL" if report["assets"] else "BLOCKED", stop_reason=video.classify_error(exc),
                      stop_evidence={"exception_type": type(exc).__name__})
    finally:
        if transport:
            report.update(request_count=transport.count, media_redirect_count=transport.redirect_count)
    return report


def run_bounded(profile, workdir, ffmpeg):
    job, process = WindowsJob(), None
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--profile", profile,
                   "--workdir", str(workdir), "--ffmpeg", str(ffmpeg)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(process)
        try:
            stdout, _ = process.communicate(input="START_U02\n", timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            job.close()  # Kills only assigned worker and its descendants, including FFmpeg/ffprobe.
            process.communicate(timeout=5)
            return {"download": "BLOCKED", "stop_reason": "worker_total_timeout", "partial_files_preserved": True}
        if process.returncode != 0:
            raise ProbeStop("worker_failed")
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ProbeStop("invalid_worker_result")
        return result
    finally:
        job.close()
        if process and process.poll() is None:
            # Assignment failure happens before START_U02, so no descendants exist.
            process.kill()
            process.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path, help="Existing ffmpeg.exe; optional sibling ffprobe.exe")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if sys.stdin.readline() != "START_U02\n":
            return 2
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = run_worker(args.profile, args.workdir, args.ffmpeg)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    try:
        ffmpeg, _ = check_ffmpeg(args.ffmpeg)
        workdir = check_workdir(args.workdir, create=True)
    except (OSError, ProbeStop):
        parser.error("Use a new .artifacts/video-download-NAME directory and explicitly supplied existing ffmpeg.exe")
    report = {"schema_version": 1, "experiment": "U02_VIDEO_DOWNLOAD", "research_gate": "NEEDS_MORE_EVIDENCE",
              "timestamp_utc": datetime.now(timezone.utc).isoformat(), "yt_dlp_required": video.PINNED_VERSION,
              "samples": [{"bvid": bvid, "part": part} for bvid, part in SAMPLES],
              "max_assets": 3, "max_height": 360, "request_limit": REQUEST_LIMIT, "worker_timeout_seconds": WORKER_TIMEOUT,
              "download": "IN_PROGRESS", "preview": "NOT_TESTED", "playback": "NOT_TESTED", "credentials_persisted": False}
    with (workdir / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle)
        handle.flush()
        try:
            report.update(run_bounded(args.profile, workdir, ffmpeg))
        except ProbeStop as exc:
            report.update(download="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
        except Exception as exc:
            report.update(download="BLOCKED", stop_reason="parent_probe_failed", stop_evidence={"exception_type": type(exc).__name__})
        handle.seek(0)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.truncate()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("download") == "PASS" and len(report.get("assets", [])) == 3 else 2


if __name__ == "__main__":
    raise SystemExit(main())
