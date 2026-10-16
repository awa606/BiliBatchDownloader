"""Bounded metadata-only research; this is not a production video adapter."""
from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

_spec = importlib.util.spec_from_file_location("_video_account_probe", Path(__file__).with_name("research_probe.py"))
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)
ProbeStop = common.ProbeStop
PINNED_VERSION = "2026.8.19"
TOTAL_TIMEOUT = 120
URL_PATTERN = re.compile(r"https://www\.bilibili\.com/video/(BV[A-Za-z0-9]{10})/?\Z")


def validate_url(url):
    match = URL_PATTERN.fullmatch(url)
    if not match:
        raise ValueError("Only a canonical HTTPS Bilibili BV video URL is accepted")
    return match[1]


def positive_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)) and re.fullmatch(r"[1-9][0-9]*", str(value)):
        return int(value)
    return None


def classify_error(exc):
    # Inspect only in memory. Never include upstream exception text in evidence.
    message = str(exc).lower()
    if any(word in message for word in ("rate limit", "riskdata", "verification", "captcha", "http error", "412", "-352", "-401")):
        return "platform_stop"
    if any(word in message for word in ("login", "logged in", "sign in", "-101", "cookie")):
        return "login_stop"
    if any(word in message for word in ("timed out", "timeout")):
        return "transport_timeout"
    return "extractor_stop"


class StopLogger(common.QuietLogger):
    def warning(self, message, *args, **kwargs):
        super().warning(message, *args, **kwargs)
        reason = classify_error(message)
        if reason in {"platform_stop", "login_stop", "transport_timeout"}:
            raise ProbeStop(reason)


def capture_pages(payload, observed):
    if not isinstance(payload, dict):
        return
    video = payload.get("videoData", payload.get("videoInfo"))
    pages = video.get("pages") if isinstance(video, dict) else None
    if pages is None and isinstance(payload.get("data"), list):
        pages = payload["data"]
    if not isinstance(pages, list) or not pages:
        return
    safe_pages = []
    for page in pages:
        if not isinstance(page, dict):
            raise ProbeStop("missing_page_identity")
        number, cid = positive_int(page.get("page")), positive_int(page.get("cid"))
        if not number or not cid:
            raise ProbeStop("missing_page_identity")
        safe_pages.append({"page": number, "cid": cid})
    if len({p["page"] for p in safe_pages}) != len(safe_pages):
        raise ProbeStop("duplicate_page_identity")
    if observed.get("pages") and observed["pages"] != safe_pages:
        raise ProbeStop("page_identity_changed_during_probe")
    observed["pages"] = safe_pages


def summarize(info, observed, bvid):
    if not isinstance(info, dict):
        raise ProbeStop("missing_video_contract")
    entries = info.get("entries") if info.get("_type") == "playlist" else [info]
    if not isinstance(entries, list) or not entries or len(entries) > 2:
        raise ProbeStop("invalid_bounded_entry_count")
    pages = observed.get("pages", [])
    if not pages:
        raise ProbeStop("missing_page_identity")
    parts = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ProbeStop("missing_video_contract")
        identifier = entry.get("id", "")
        if identifier not in {bvid, f"{bvid}_p{index}"}:
            raise ProbeStop("unexpected_video_identity")
        uid = positive_int(entry.get("uploader_id"))
        if not uid:
            raise ProbeStop("missing_author_identity")
        page = next((p for p in pages if p["page"] == index), None)
        if not page:
            raise ProbeStop("missing_page_identity")
        tags = entry.get("tags")
        tags_valid = isinstance(tags, list) and all(isinstance(t, str) for t in tags)
        safe_tags = [re.sub(r"https?://\S+", "[REDACTED_URL]", t)[:256] for t in tags[:100]] if tags_valid else []
        parts.append({"video_id": identifier, "author_uid": str(uid), **page,
                      "tags": safe_tags, "tags_status": "present" if safe_tags else "empty" if tags_valid else "missing",
                      "thumbnail_present": bool(entry.get("thumbnail")),
                      "description_present": bool(entry.get("description")),
                      "format_count": len(entry.get("formats") or [])})
    if len(parts) != min(len(pages), 2):
        raise ProbeStop("incomplete_selected_parts", {"extracted_parts": len(parts), "expected_parts": min(len(pages), 2)})
    if any(p["tags_status"] == "missing" or not p["thumbnail_present"] or not p["format_count"] for p in parts):
        raise ProbeStop("missing_video_metadata", {"parts": parts})
    return {"video_id": bvid, "part_count": len(pages), "all_part_identities": pages,
            "sampled_part_count": len(parts), "parts": parts,
            "metadata_scope": "first_two_parts_only" if len(pages) > 2 else "all_parts"}


def extract_metadata(url, jar):
    from yt_dlp import YoutubeDL
    from yt_dlp.cookies import YoutubeDLCookieJar
    from yt_dlp.extractor.bilibili import BiliBiliIE

    observed = {}

    class ResearchBiliIE(BiliBiliIE):
        @classmethod
        def ie_key(cls):
            return BiliBiliIE.ie_key()

        def _download_json(self, *args, **kwargs):
            result = super()._download_json(*args, **kwargs)
            if isinstance(result, dict) and type(result.get("code")) is int and result["code"] != 0:
                raise ProbeStop("login_or_platform_stop" if result["code"] in common.STOP_CODES else "api_error",
                                {"api_code": result["code"]})
            if args and "/x/player/pagelist" in str(args[0]):
                capture_pages(result, observed)
            return result

        def _search_json(self, *args, **kwargs):
            result = super()._search_json(*args, **kwargs)
            capture_pages(result, observed)
            return result

    options = {"quiet": True, "no_warnings": False, "logger": StopLogger(),
               "retries": 0, "fragment_retries": 0, "extractor_retries": 0,
               "socket_timeout": 15, "skip_download": True, "noplaylist": False,
               "playlistend": 2, "playlist_items": "1:2", "ignoreerrors": False,
               "cachedir": False, "writeinfojson": False, "writethumbnail": False,
               "writesubtitles": False, "getcomments": False}
    with YoutubeDL(options, auto_init=False) as ydl:
        # yt-dlp requires its extended CookieJar API; copy only the already
        # filtered Bilibili cookies and do not give it any persistence filename.
        memory_jar = YoutubeDLCookieJar()
        for cookie in jar:
            memory_jar.set_cookie(cookie)
        ydl.cookiejar = memory_jar
        ydl.add_info_extractor(ResearchBiliIE())
        info = ydl.extract_info(url, download=False)
    return summarize(info, observed, validate_url(url))


def run_worker(profile, url):
    report = {"account": "NOT_TESTED", "metadata": "NOT_TESTED"}
    try:
        if importlib.metadata.version("yt-dlp") != PINNED_VERSION:
            raise ProbeStop("dependency_version_mismatch")
        jar, evidence = common.load_bili_cookies(profile, None)
        report["cookie_evidence"] = evidence
        client = common.BoundedClient(jar)
        account = common.run_account(client.get)
        report.update(account)
        report["account_request_count"] = client.request_count
        if account.get("account") != "PASS" or "stop_reason" in account:
            return report
        report.update(extract_metadata(url, jar))
        report["metadata"] = "PASS"
    except ProbeStop as exc:
        report.update(metadata="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
    except Exception as exc:
        report.update(metadata="BLOCKED", stop_reason=classify_error(exc),
                      stop_evidence={"exception_type": type(exc).__name__})
    return report


def run_bounded(profile, url):
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--profile", profile, "--url", url]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", creationflags=flags)
    try:
        stdout, _ = process.communicate(timeout=TOTAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()  # Metadata-only worker starts no downloader/FFmpeg children.
        process.communicate(timeout=10)
        return {"account": "UNKNOWN", "metadata": "BLOCKED", "stop_reason": "worker_total_timeout"}
    if process.returncode != 0:
        return {"account": "UNKNOWN", "metadata": "BLOCKED", "stop_reason": "worker_failed"}
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeDecodeError):
        return {"account": "UNKNOWN", "metadata": "BLOCKED", "stop_reason": "invalid_worker_result"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Explicit dedicated Chrome profile; never saved")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        bvid = validate_url(args.url)
    except ValueError:
        parser.error("Only a canonical HTTPS Bilibili BV video URL is accepted")
    if args.worker:
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = run_worker(args.profile, args.url)
        # ASCII JSON avoids console codepage dependence; raw upstream output never escapes.
        print(json.dumps(result, ensure_ascii=True))
        return 0
    if args.output is None:
        parser.error("--output must name a new evidence file inside .artifacts")
    report = {"schema_version": 1, "experiment": "VIDEO_METADATA", "video_id": bvid,
              "timestamp_utc": datetime.now(timezone.utc).isoformat(), "yt_dlp_required": PINNED_VERSION,
              "research_gate": "NEEDS_MORE_EVIDENCE", "media_downloads": 0,
              "download_probe": "NOT_IMPLEMENTED", "credential_values_persisted": False,
              "max_sampled_parts": 2, "socket_timeout_seconds": 15, "total_timeout_seconds": TOTAL_TIMEOUT}
    try:
        handle = common.open_evidence(args.output)
    except (ValueError, OSError):
        parser.error("Output must be a new writable file inside .artifacts; no credentials read or requests sent")
    with handle:
        json.dump({**report, "status": "IN_PROGRESS"}, handle)
        handle.flush()
        try:
            report.update(run_bounded(args.profile, args.url))
        except Exception as exc:
            report.update(metadata="BLOCKED", stop_reason="worker_launch_or_wait_failed",
                          stop_evidence={"exception_type": type(exc).__name__})
        handle.seek(0)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.truncate()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("metadata") == "PASS" and "stop_reason" not in report else 2


if __name__ == "__main__":
    raise SystemExit(main())
