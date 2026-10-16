"""U02: nav and one fixed webpage, optionally one strict canonical 301; no media."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

_spec = importlib.util.spec_from_file_location("_page_download_probe", Path(__file__).with_name("video_download_probe.py"))
download = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(download)
common, ProbeStop = download.common, download.ProbeStop
FIXED_BVID = "BV13x41117TL"
FIXED_URL = download.sample_url(FIXED_BVID, 1)
REQUEST_LIMIT = 2
WORKER_TIMEOUT = 60


class PageTransport(download.BoundedTransport):
    def __init__(self, jar, initial_requests=0, *, allow_canonical_redirect=False):
        super().__init__(jar, initial_requests, allow_canonical_redirect=allow_canonical_redirect,
                         request_limit=REQUEST_LIMIT + int(allow_canonical_redirect))

    def allow_media(self, urls):
        raise ProbeStop("page_probe_forbids_media")

    def request(self, request, *, default_headers=None):
        from yt_dlp.networking import Request
        if isinstance(request, str):
            request = Request(request)
        if not isinstance(request, Request) or request.url != FIXED_URL:
            raise ProbeStop("fixed_page_only")
        if self.count >= self.request_limit:
            raise ProbeStop("request_budget_exhausted")
        # The parent checks the budget before every physical request, including the opt-in hop.
        return super().request(request, default_headers=default_headers)


def page_identity(body):
    match = re.search(rb"window\.__INITIAL_STATE__\s*=\s*", body)
    if not match:
        raise ProbeStop("page_identity_missing")
    try:
        initial, _ = json.JSONDecoder().raw_decode(body[match.end():].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProbeStop("page_initial_state_invalid") from None
    data = initial.get("videoData") if isinstance(initial, dict) else None
    if not isinstance(data, dict) or data.get("bvid") != FIXED_BVID:
        raise ProbeStop("page_identity_mismatch")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ProbeStop("page_title_missing")
    return {"bvid": FIXED_BVID, "title_present": True}


def run_worker(profile, *, browser_confirmed, allow_canonical_redirect=False):
    report = {"account": "NOT_TESTED", "page": "NOT_TESTED", "request_count": 0,
              "media_downloads": 0, "browser_prerequisite": "EXTERNALLY_CONFIRMED" if browser_confirmed else "MISSING"}
    client = transport = None
    try:
        if not browser_confirmed:
            raise ProbeStop("browser_prerequisite_missing")
        if importlib.metadata.version("yt-dlp") != download.video.PINNED_VERSION:
            raise ProbeStop("dependency_version_mismatch")
        jar, evidence = common.load_bili_cookies(profile, None)
        report["cookie_evidence"] = evidence
        client = common.BoundedClient(jar, max_requests=1)
        report.update(common.run_account(client.get))
        if report.get("account") != "PASS" or "stop_reason" in report:
            report["page"] = "BLOCKED"
            return report
        if client.request_count != 1:
            raise ProbeStop("account_request_count_mismatch")
        from yt_dlp import YoutubeDL
        from yt_dlp.networking import Request
        transport = PageTransport(jar, initial_requests=client.request_count, allow_canonical_redirect=allow_canonical_redirect)
        with YoutubeDL({"quiet": True, "cachedir": False, "logger": common.QuietLogger()}, auto_init=False) as ydl:
            response = transport.request(Request(FIXED_URL), default_headers=ydl.params["http_headers"])
            try:
                body = response.read(download.MAX_METADATA_BYTES + 1)
                report["page_http_status"] = response.status
                report["page_final_url"] = response.url  # Only the fixed URL or its strictly validated slash target.
            finally:
                response.close()
        report["page_response_sha256"] = hashlib.sha256(body).hexdigest()
        report["page_identity"] = page_identity(body)
        report["page"] = "PASS"
    except ProbeStop as exc:
        report.update(page="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
    except Exception as exc:
        report.update(page="BLOCKED", stop_reason="page_probe_failed", stop_evidence={"exception_type": type(exc).__name__})
    finally:
        report["request_count"] = transport.count if transport else client.request_count if client else 0
        if transport:
            report.update(page_redirect_count=transport.page_redirect_count, page_redirect_events=transport.page_redirect_events,
                          media_redirect_count=transport.redirect_count)
    return report


def run_bounded(profile, *, allow_canonical_redirect=False):
    job, process = download.WindowsJob(), None
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--browser-confirmed", "--profile", profile]
        if allow_canonical_redirect:
            command.append("--allow-canonical-redirect")
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(process)
        try:
            stdout, _ = process.communicate(input="START_U02_PAGE\n", timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            job.close()
            process.communicate(timeout=5)
            return {"page": "BLOCKED", "stop_reason": "worker_total_timeout", "request_count": None,
                    "request_count_status": "UNKNOWN_AFTER_TIMEOUT", "media_downloads": 0}
        if process.returncode != 0:
            raise ProbeStop("worker_failed")
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ProbeStop("invalid_worker_result")
        return result
    finally:
        job.close()
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--browser-confirmed", action="store_true", help="Normal browser access was independently confirmed")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-canonical-redirect", action="store_true", help="Allow one strictly validated 301 slash redirect")
    args = parser.parse_args(argv)
    if not args.browser_confirmed or (not args.worker and args.output is None):
        parser.error("Requires confirmed normal browser access and a new evidence output")
    if args.worker:
        if sys.stdin.readline() != "START_U02_PAGE\n":
            return 2
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = run_worker(args.profile, browser_confirmed=True, allow_canonical_redirect=args.allow_canonical_redirect)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    try:
        output = common.open_evidence(args.output)
    except (ValueError, OSError):
        parser.error("Evidence must be a new writable file inside .artifacts; no credentials read or requests sent")
    report = {"schema_version": 1, "experiment": "U02_FIXED_PAGE_AFTER_HEADER_REPAIR", "bvid": FIXED_BVID,
              "timestamp_utc": datetime.now(timezone.utc).isoformat(), "research_gate": "NEEDS_MORE_EVIDENCE",
              "request_limit": REQUEST_LIMIT + int(args.allow_canonical_redirect), "worker_timeout_seconds": WORKER_TIMEOUT,
              "allow_canonical_redirect": args.allow_canonical_redirect,
              "yt_dlp_required": download.video.PINNED_VERSION, "browser_prerequisite": "EXTERNALLY_CONFIRMED",
              "page": "IN_PROGRESS", "media_downloads": 0, "credentials_persisted": False,
              "download": "NOT_TESTED", "playback": "NOT_TESTED", "http412_causation": "NOT_PROVEN"}
    with output:
        json.dump(report, output)
        output.flush()
        try:
            report.update(run_bounded(args.profile, allow_canonical_redirect=args.allow_canonical_redirect))
        except ProbeStop as exc:
            report.update(page="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence, request_count=None)
        except Exception as exc:
            report.update(page="BLOCKED", stop_reason="parent_page_probe_failed",
                          stop_evidence={"exception_type": type(exc).__name__}, request_count=None)
        output.seek(0)
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.truncate()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("page") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
