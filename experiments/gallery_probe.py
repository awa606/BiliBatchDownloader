"""Isolated gallery-dl metadata experiment, not a production downloader."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from research_probe import BoundedClient, ProbeStop, load_bili_cookies, open_evidence, run_account

PATTERN = re.compile(r"https://(?:www\.bilibili\.com/opus|t\.bilibili\.com)/(\d+)$")


def normalized_url(value):
    match = PATTERN.fullmatch(value)
    if not match:
        raise ValueError("Expected a Bilibili opus/dynamic numeric URL without query")
    return "https://www.bilibili.com/opus/" + match[1]


def summarize_message(message):
    kind, url, metadata = message
    if kind != 3:  # gallery_dl.extractor.common.Message.Url
        return None
    parsed = urlsplit(url)
    return {
        "sequence": metadata.get("num"), "suffix": str(metadata.get("suffix", ""))[:10],
        "format": Path(parsed.path).suffix.lstrip(".").lower(),
        "source_path_sha256": hashlib.sha256(parsed.path.encode()).hexdigest(),
        "width": metadata.get("width"), "height": metadata.get("height"),
    }


def author_identity(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ProbeStop("missing_author_identity")
    return str(value)


def safe_http_failure(exc):
    response = getattr(exc, "response", None)
    evidence = {"exception_type": type(exc).__name__}
    if response is not None:
        status = getattr(response, "status_code", None)
        if type(status) is int:
            evidence["http_status"] = status
        body = getattr(response, "content", None)
        if isinstance(body, bytes):
            evidence["response_sha256"] = hashlib.sha256(body).hexdigest()
    return ProbeStop("http_stop" if "http_status" in evidence else "gallery_transport_stop", evidence)


def child_probe(profile, url):
    report = {"experiment": "U03_gallery_metadata", "status": "NOT_TESTED", "assets": [],
              "media_downloads": 0, "download_probe": "NOT_RUN", "research_gate": "NEEDS_MORE_EVIDENCE"}
    logging.disable(logging.CRITICAL)
    request_count = 0
    try:
        jar, _ = load_bili_cookies(profile, None)
        account_client = BoundedClient(jar, max_requests=1)
        account = run_account(account_client.get)
        request_count += account_client.request_count
        if account.get("account") != "PASS":
            report.update(status="BLOCKED", stop_reason=account.get("stop_reason"), stop_evidence=account.get("stop_evidence", {}))
            return report
        from gallery_dl import config, extractor
        from importlib.metadata import version
        config.set(("extractor",), "retries", 0)
        config.set(("extractor",), "timeout", 15)
        config.set(("extractor",), "sleep-request", 1)
        item = extractor.find(url)
        if item is None:
            raise ProbeStop("extractor_not_found")
        item.initialize()
        item.session.cookies.update(jar)
        item.session.max_redirects = 0
        original_request = item.request
        def bounded_request(target, *args, **kwargs):
            nonlocal request_count
            if target != url:
                raise ProbeStop("unexpected_gallery_endpoint")
            if request_count >= 2:
                raise ProbeStop("request_budget_exhausted")
            request_count += 1
            try:
                response = original_request(target, *args, **kwargs)
            except Exception as exc:
                raise safe_http_failure(exc) from None
            if response.status_code != 200:
                raise ProbeStop("http_stop", {"http_status": response.status_code})
            body = response.content
            report["response_sha256"] = hashlib.sha256(body).hexdigest()
            if b"window._riskdata_" in body:
                raise ProbeStop("verification_page_detected")
            return response
        item.request = bounded_request
        original_wait = item.wait
        def bounded_wait(*args, **kwargs):
            seconds = kwargs.get("seconds", args[0] if args else 0)
            if isinstance(seconds, (int, float)) and seconds > 15:
                raise ProbeStop("upstream_long_wait_blocked")
            return original_wait(*args, **kwargs)
        item.wait = bounded_wait
        report["gallery_dl"] = version("gallery-dl")
        # Upstream logger/output is discarded even if a dependency writes directly.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for message in item:
                if message[0] == 2:  # Directory metadata
                    metadata = message[2]
                    report.update(author_uid=author_identity(metadata.get("user_id")),
                                  declared_images=metadata.get("count"),
                                  body_present=bool(metadata.get("content")),
                                  title_present=bool(metadata.get("title")))
                summary = summarize_message(message)
                if summary:
                    report["assets"].append(summary)
                if len(report["assets"]) > 20:
                    raise ProbeStop("asset_budget_exceeded")
        if not report.get("author_uid") or not report["assets"]:
            raise ProbeStop("missing_gallery_contract")
        report["status"] = "METADATA_EXTRACTED_NOT_MEDIA_VERIFIED"
    except ProbeStop as exc:
        report.update(status="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
    except Exception as exc:
        report.update(status="FAIL", stop_reason="extractor_error", exception_type=type(exc).__name__)
    finally:
        report["network_requests"] = request_count
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        url = normalized_url(args.url)
    except ValueError:
        parser.error("Invalid fixed sample URL")
    if args._child:
        print(json.dumps(child_probe(args.profile, url), ensure_ascii=False))
        return 0
    if args.output is None:
        parser.error("--output is required")
    try:
        handle = open_evidence(args.output)
    except (ValueError, OSError):
        parser.error("Output must be a new writable file inside .artifacts")
    with handle:
        report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "sample_url": url,
                  "status": "IN_PROGRESS", "research_gate": "NEEDS_MORE_EVIDENCE"}
        json.dump(report, handle)
        handle.flush()
        try:
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_child", "--profile", args.profile,
                                      "--url", url], capture_output=True, text=True, encoding="utf-8", timeout=45,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            report.update(json.loads(process.stdout))
        except subprocess.TimeoutExpired:
            report.update(status="BLOCKED", stop_reason="child_timeout_45s")
        except Exception as exc:
            report.update(status="FAIL", stop_reason="child_protocol_error", exception_type=type(exc).__name__)
        handle.seek(0)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.truncate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "METADATA_EXTRACTED_NOT_MEDIA_VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
