"""Bounded, read-only account research. Never print or persist credentials."""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.bilibili.com"
STOP_CODES = {-101, -111, -401, -352, -412, 22115}
MAX_RESPONSE = 2 * 1024 * 1024


class ProbeStop(Exception):
    def __init__(self, reason: str, evidence: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence or {}


class QuietLogger:
    """Discard upstream messages: they can contain private paths and data."""
    def __init__(self):
        self.warning_count = 0
        self.error_count = 0

    def debug(self, message, *args, **kwargs):
        pass

    info = debug

    def warning(self, message, *args, **kwargs):
        self.warning_count += 1

    def error(self, message, *args, **kwargs):
        self.error_count += 1


def load_bili_cookies(profile: str | None, cookie_file: str | None):
    logger = QuietLogger()
    try:
        if profile:
            from yt_dlp.cookies import extract_cookies_from_browser
            source = extract_cookies_from_browser("chrome", profile, logger=logger)
        else:
            source = http.cookiejar.MozillaCookieJar(cookie_file)
            source.load(ignore_discard=True, ignore_expires=False)
    except Exception as exc:
        raise ProbeStop("cookie_load_failed", {
            "exception_type": type(exc).__name__,
            "warning_count": logger.warning_count,
            "error_count": logger.error_count,
        }) from None
    jar = http.cookiejar.CookieJar()
    for cookie in source:
        domain = cookie.domain.lstrip(".").lower()
        if (domain == "bilibili.com" or domain.endswith(".bilibili.com")) and not cookie.is_expired():
            jar.set_cookie(cookie)
    evidence = {"bilibili_cookie_count": len(jar), "warning_count": logger.warning_count,
                "error_count": logger.error_count}
    if not any(c.name == "SESSDATA" and c.value for c in jar):
        raise ProbeStop("session_cookie_unavailable", evidence)
    return jar, evidence


def classify_response(status: int, body: bytes) -> dict[str, Any]:
    evidence = {"http_status": status, "response_sha256": hashlib.sha256(body).hexdigest()}
    if status != 200:
        raise ProbeStop("http_stop", evidence)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ProbeStop("non_json_or_verification_page", evidence) from None
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise ProbeStop("missing_response_contract", evidence)
    evidence["api_code"] = payload["code"]
    if payload["code"] != 0:
        raise ProbeStop("login_or_platform_stop" if payload["code"] in STOP_CODES else "api_error", evidence)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProbeStop("missing_data_contract", evidence)
    return {"data": data, "evidence": evidence}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeStop("redirect_blocked", {"http_status": code})


class BoundedClient:
    allowed_paths = {"/x/web-interface/nav", "/x/relation/followings"}

    def __init__(self, jar: http.cookiejar.CookieJar, max_requests: int = 5):
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
        self.request_count = 0
        self.max_requests = max_requests
        self.last_request_at = 0.0

    def get(self, path: str, params: dict[str, Any] | None = None):
        if path not in self.allowed_paths:
            raise ProbeStop("endpoint_not_allowed")
        if self.request_count >= self.max_requests:
            raise ProbeStop("request_budget_exhausted")
        if self.request_count:
            time.sleep(max(0.0, 1.0 - (time.monotonic() - self.last_request_at)))
        self.request_count += 1
        self.last_request_at = time.monotonic()
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/",
            "Accept": "application/json",
        })
        try:
            with self.opener.open(request, timeout=15) as response:
                body = response.read(MAX_RESPONSE + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            raise ProbeStop("http_stop", {"http_status": exc.code}) from None
        except ProbeStop:
            raise
        except Exception as exc:
            raise ProbeStop("transport_stop", {"exception_type": type(exc).__name__}) from None
        if len(body) > MAX_RESPONSE:
            raise ProbeStop("response_too_large")
        return classify_response(status, body)


def run_account(get: Callable, *, followings: bool = False, full_pages: int | None = None) -> dict[str, Any]:
    result = {"account": "NOT_TESTED", "followings": "NOT_TESTED", "pages": []}
    try:
        nav = get("/x/web-interface/nav")
        result["nav_evidence"] = nav["evidence"]
        data = nav["data"]
        if data.get("isLogin") is not True:
            raise ProbeStop("not_logged_in")
        uid = data.get("mid")
        if type(uid) is not int or uid <= 0:
            raise ProbeStop("missing_account_identity")
        result["account"] = "PASS"
        if not followings:
            return result
        unique_ids: set[int] = set()
        total = None
        pages = list(range(1, full_pages + 1)) if full_pages else [1, 2]
        index = 0
        duplicates = 0
        while index < len(pages):
            page = pages[index]
            index += 1
            response = get("/x/relation/followings", {"vmid": uid, "pn": page, "ps": 50, "order": "desc"})
            data = response["data"]
            rows, current_total = data.get("list"), data.get("total")
            if not isinstance(rows, list) or type(current_total) is not int or current_total < 0:
                raise ProbeStop("missing_followings_contract")
            if total is not None and total != current_total:
                raise ProbeStop("followings_changed_during_probe")
            total = current_total
            for row in rows:
                mid = row.get("mid") if isinstance(row, dict) else None
                if type(mid) is not int or mid <= 0:
                    raise ProbeStop("missing_following_identity")
                duplicates += int(mid in unique_ids)
                unique_ids.add(mid)
            result["pages"].append({"page": page, "count": len(rows), **response["evidence"]})
            result.update(platform_total=total, unique_count=len(unique_ids), duplicate_count=duplicates)
            if not full_pages and total > 250 and 5 not in pages:
                pages.extend([5, 6])
            if len(unique_ids) >= total:
                break
            if not rows:
                raise ProbeStop("premature_empty_page")
        complete = len(unique_ids) == total and duplicates == 0
        result["followings"] = "PASS" if complete else "PARTIAL"
        result["scope"] = "complete" if complete else "bounded_pages_only"
    except ProbeStop as exc:
        result.update(stop_reason=exc.reason, stop_evidence=exc.evidence)
        if result["account"] != "PASS":
            result["account"] = "BLOCKED"
        elif followings:
            result["followings"] = "PARTIAL" if result["pages"] else "BLOCKED"
    return result


def open_evidence(output: Path):
    output = output.resolve()
    artifacts = (ROOT / ".artifacts").resolve()
    if not artifacts.is_relative_to(ROOT.resolve()) or not output.is_relative_to(artifacts):
        raise ValueError("Evidence output must stay inside .artifacts")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.open("x", encoding="utf-8")


def save_exclusive(report: dict, output: Path):
    with open_evidence(output) as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--profile", help="Explicit dedicated Chrome profile; never saved in report")
    source.add_argument("--cookie-file", help="Explicit user-provided Netscape cookie file; never saved")
    parser.add_argument("--followings", action="store_true")
    parser.add_argument("--full-pages", type=int, help="After boundary validation, fixed full traversal cap (1..20)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.full_pages is not None and (not args.followings or not 1 <= args.full_pages <= 20):
        parser.error("--full-pages needs --followings and must be between 1 and 20")
    import importlib.metadata
    report = {"schema_version": 1, "experiment": "U01", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "python": sys.version.split()[0], "yt_dlp": importlib.metadata.version("yt-dlp"),
              "research_gate": "NEEDS_MORE_EVIDENCE", "network_requests": 0,
              "media_downloads": 0, "credential_values_persisted": False}
    try:
        output_handle = open_evidence(args.output)
    except (ValueError, OSError):
        parser.error("Output must be a new writable file inside .artifacts; no credentials read or requests sent")
    with output_handle:
        # Reserve evidence before touching credentials or network. A process crash
        # leaves IN_PROGRESS, never a false successful result.
        json.dump({**report, "status": "IN_PROGRESS"}, output_handle)
        output_handle.flush()
        client = None
        try:
            jar, evidence = load_bili_cookies(args.profile, args.cookie_file)
            report["cookie_evidence"] = evidence
            client = BoundedClient(jar, max_requests=1 + args.full_pages if args.full_pages else 5)
            report.update(run_account(client.get, followings=args.followings, full_pages=args.full_pages))
        except ProbeStop as exc:
            report.update(account="BLOCKED", followings="NOT_TESTED", stop_reason=exc.reason, stop_evidence=exc.evidence)
        except Exception as exc:
            report.update(account="BLOCKED", followings="NOT_TESTED", stop_reason="unexpected_probe_error",
                          stop_evidence={"exception_type": type(exc).__name__})
        finally:
            report["network_requests"] = client.request_count if client else 0
        output_handle.seek(0)
        json.dump(report, output_handle, ensure_ascii=False, indent=2)
        output_handle.write("\n")
        output_handle.truncate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    passed = report.get("account") == "PASS" and "stop_reason" not in report
    if args.followings:
        passed = passed and report.get("followings") == "PASS"
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
