"""One fixed U03 page diagnosis: nav + source, no media and no download release."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import gallery_download_probe as gallery
from research_probe import BoundedClient, ProbeStop, QuietLogger, load_bili_cookies, run_account

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = "1249456774436618276"
REQUEST_LIMIT = 2
WORKER_TIMEOUT = 60
START_TOKEN = "START_U03_PAGE_DIAGNOSIS\n"
MARKER = "module_blocked"
ASSIGNMENT = "window.__INITIAL_STATE__="
WindowsJob = gallery.WindowsJob


def fingerprint(path):
    with path.open("rb") as handle:
        raw = handle.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ProbeStop("history_report_too_large")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def load_history(baseline_path, failure_path):
    try:
        baseline = gallery.load_baseline(baseline_path, SAMPLE)
        metadata, baseline_sha = fingerprint(baseline_path)
        failure, failure_sha = fingerprint(failure_path)
        digest = failure.get("source_response_sha256")
        decoding = failure.get("source_metadata_decoding", {})
        if (baseline_sha != baseline["sha256"] or not isinstance(digest, str)
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or failure.get("experiment") != "U03_GALLERY_DOWNLOAD"
                or failure.get("sample_dynamic_id") != SAMPLE
                or failure.get("stop_reason") != "blocked_gallery_module"
                or failure.get("network_requests") != 2 or failure.get("media_requests") != 0
                or failure.get("total_media_bytes") != 0 or failure.get("assets") != []
                or failure.get("baseline_report_sha256") != baseline_sha
                or metadata.get("response_sha256") != digest
                or decoding.get("raw_sha256") != digest
                or decoding.get("decoded_sha256") != digest
                or decoding.get("status") != "PASS_THROUGH"):
            raise ProbeStop("history_contract_mismatch")
        return baseline, {"baseline_report_sha256": baseline_sha, "prior_failure_report_sha256": failure_sha,
                          "historical_raw_sha256": digest, "historical_decoded_sha256": digest}
    except ProbeStop:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise ProbeStop("history_report_invalid") from None


def typed(value, present=True):
    kind = ("MISSING" if not present else "NULL" if value is None else "BOOLEAN" if isinstance(value, bool)
            else "OBJECT" if isinstance(value, dict) else "ARRAY" if isinstance(value, list)
            else "STRING" if isinstance(value, str) else "NUMBER" if isinstance(value, (int, float)) else "OTHER")
    return {"key_present": present, "value_type": kind, "truthy": bool(value) if present else False}


class Pairs(dict):
    """Preserve duplicate JSON members for observation, never replace upstream parsing."""
    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = pairs


def audit_state(page):
    # Select exactly the slice used by the pinned upstream BilibiliAPI.article.
    from gallery_dl import text
    result = {"status": "NOT_PARSED", "whole_page_literal_count": page.count('"module_blocked"'),
              "assignment_count": page.count(ASSIGNMENT), "selected_state_literal_count": None,
              "outside_selected_state_literal_count": None, "key_occurrences": [],
              "string_value_marker_count": 0, "duplicate_member_count": 0,
              "duplicate_blocked_member_count": 0}
    try:
        first = page.index(ASSIGNMENT) + len(ASSIGNMENT)
        last = page.index("};", first) + 1
        selected = text.extr(page, ASSIGNMENT, "};") + "}"
        result["selected_state_literal_count"] = selected.count('"module_blocked"')
        result["outside_selected_state_literal_count"] = (page[:first] + page[last:]).count('"module_blocked"')
        state = json.loads(selected, object_pairs_hook=Pairs)
    except (ValueError, RecursionError):
        result["status"] = "PARSE_FAILED"
        return result
    stack, nodes = [(state, (), 0)], 0
    while stack:
        value, path, depth = stack.pop()
        nodes += 1
        if nodes > 20000 or depth > 64 or len(result["key_occurrences"]) > 128:
            result["status"] = "DIAGNOSTIC_LIMIT"
            return result
        if isinstance(value, Pairs):
            seen = set()
            for key, child in value.pairs:
                duplicate = key in seen
                seen.add(key)
                result["duplicate_member_count"] += int(duplicate)
                if key == MARKER:
                    module_field = len(path) == 3 and path[:2] == ("detail", "modules") and type(path[2]) is int
                    result["key_occurrences"].append({"location": "MODULE_FIELD" if module_field else "STATE_OTHER_FIELD",
                        "module_index": path[2] if module_field else None, "duplicate_in_object": duplicate, **typed(child)})
                    result["duplicate_blocked_member_count"] += int(duplicate)
                stack.append((child, path + (key,), depth + 1))
        elif isinstance(value, list):
            stack.extend((child, path + (index,), depth + 1) for index, child in enumerate(value))
        elif isinstance(value, str):
            result["string_value_marker_count"] += value.count(MARKER)
    result["status"] = "PARSED"
    return result


def original_modules(article):
    detail = article.get("detail") if isinstance(article, dict) else None
    modules = detail.get("modules") if isinstance(detail, dict) else None
    result = {"status": "INVALID", "module_count": None, "blocked_fields": [], "merge_overwrite_count": 0}
    if not isinstance(modules, list) or len(modules) > 256 or any(not isinstance(m, dict) for m in modules):
        return result
    result.update(status="OBSERVED", module_count=len(modules))
    for index, module in enumerate(modules):
        if MARKER in module:
            result["blocked_fields"].append({"module_index": index, **typed(module[MARKER])})
    result["merge_overwrite_count"] = max(0, len(result["blocked_fields"]) - 1)
    return result


class PageTransport(gallery.Transport):
    def __init__(self, jar, deadline, *, account_requests=1, last_request_at=None):
        super().__init__(jar, SAMPLE, deadline, account_requests=account_requests, last_request_at=last_request_at)
        self.request_limit = REQUEST_LIMIT
        self.state_audit = self.original = self.flattened = None
        self.source_http_status = None

    def open(self, url, *, media):
        if media:
            raise ProbeStop("page_probe_forbids_media")
        if url != self.page:
            raise ProbeStop("fixed_page_only")
        response = super().open(url, media=False)
        self.source_http_status = response.status
        return response

    def allow_assets(self, assets):
        raise ProbeStop("page_probe_forbids_media")

    def download(self, *args, **kwargs):
        raise ProbeStop("page_probe_forbids_media")

    def fetch_page(self, target, *args, **kwargs):
        response = super().fetch_page(target, *args, **kwargs)
        self.state_audit = audit_state(response.text)
        if self.state_audit["status"] == "DIAGNOSTIC_LIMIT":
            raise ProbeStop("structure_diagnostic_limit")
        return response

    def observe_directory_modules(self, metadata):
        detail = metadata.get("detail")
        modules = detail.get("modules") if isinstance(detail, dict) else None
        if not isinstance(modules, dict):
            raise ProbeStop("gallery_directory_modules_invalid")
        self.flattened = {"status": "OBSERVED", **typed(modules.get(MARKER), MARKER in modules)}
        rows = self.original["blocked_fields"] if self.original else []
        expected = {k: rows[-1][k] for k in ("key_present", "value_type", "truthy")} if rows else typed(None, False)
        self.flattened["matches_last_original_field"] = all(self.flattened[k] == v for k, v in expected.items())
        # Diagnostic-only traversal may inspect URL *metadata*, but no URL can be fetched.


def extract_metadata(transport, baseline):
    from gallery_dl import config, extractor
    config.set(("extractor",), "retries", 0)
    config.set(("extractor",), "timeout", 15)
    config.set(("extractor", "bilibili"), "livephoto", True)
    item = extractor.find(gallery.source_url(SAMPLE))
    if item is None or type(item).__name__ != "BilibiliArticleExtractor" or item.groups[0] != SAMPLE:
        raise ProbeStop("unexpected_gallery_extractor")
    item.log = QuietLogger()
    item.request = transport.fetch_page
    def no_wait(*args, **kwargs):
        raise ProbeStop("upstream_wait_blocked")
    item.wait = no_wait
    item.initialize()
    upstream_article = item.api.article
    def observe_article(article_id):
        if article_id != SAMPLE or transport.original is not None:
            raise ProbeStop("unexpected_article_call")
        article = upstream_article(article_id)
        transport.original = original_modules(article)
        if transport.original["status"] != "OBSERVED":
            raise ProbeStop("original_modules_invalid")
        return article
    item.api.article = observe_article
    assets, identity, declared = [], None, None
    for message in item:
        if message[0] == 2:
            if identity is not None:
                raise ProbeStop("multiple_directory_messages")
            identity = gallery.author_identity(message[2].get("user_id"))
            declared = message[2].get("count")
            transport.observe_directory_modules(message[2])
        elif message[0] == 3:
            assets.append(gallery.asset_descriptor(SAMPLE, message[1], message[2]))
            if len(assets) > gallery.MAX_ASSETS:
                raise ProbeStop("asset_budget_exhausted")
        else:
            raise ProbeStop("unexpected_gallery_message")
    if identity is None or transport.page_requests != 1:
        raise ProbeStop("missing_gallery_contract")
    gallery.validate_assets(SAMPLE, assets, declared)
    gallery.validate_baseline_assets(assets, identity, baseline)
    return {"author_matches": True, "image_count": declared, "ordered_media_path_hashes_match": True}


def conclude(report, transport, history):
    audit, original, flat = transport.state_audit, transport.original, transport.flattened
    report.update(source_http_status=transport.source_http_status, source_response_sha256=transport.page_sha256,
                  source_metadata_decoding=transport.page_decoding, source_structure=audit,
                  original_modules=original, directory_modules=flat)
    complete = bool(audit and audit["status"] == "PARSED" and original and original["status"] == "OBSERVED"
                    and flat and flat["matches_last_original_field"])
    report["diagnosis"] = "COMPLETE" if complete else "INCOMPLETE"
    if original and original["status"] == "OBSERVED":
        rows = original["blocked_fields"]
        effective = any(r["truthy"] and r["value_type"] == "OBJECT" for r in rows)
        ambiguous = any(r["truthy"] and r["value_type"] != "OBJECT" for r in rows)
        report["blocking_module"] = "PRESENT" if effective else "UNRESOLVED_TYPE" if ambiguous else "NO_EFFECTIVE_MODULE"
    same_raw = transport.page_sha256 == history["historical_raw_sha256"]
    same_decoded = bool(transport.page_decoding and transport.page_decoding.get("decoded_sha256") == history["historical_decoded_sha256"])
    report.update(historical_raw_matches=same_raw, historical_decoded_matches=same_decoded)
    if complete and same_raw and same_decoded and audit["whole_page_literal_count"]:
        report["historical_cause"] = ("LITERAL_SCAN_WITH_EFFECTIVE_MODULE" if report["blocking_module"] == "PRESENT" else
            "LITERAL_SCAN_WITHOUT_EFFECTIVE_MODULE" if report["blocking_module"] == "NO_EFFECTIVE_MODULE" else "UNRESOLVED")


def run_worker(profile, baseline_path, failure_path, expected_hashes):
    report = {"account": "NOT_TESTED", "diagnosis": "INCOMPLETE", "blocking_module": "NOT_OBSERVED",
              "baseline": "NOT_CHECKED", "historical_cause": "UNRESOLVED", "network_requests": 0,
              "source_requests": 0, "media_requests": 0, "total_media_bytes": 0, "download": "NOT_RUN"}
    client = transport = None
    try:
        baseline, history = load_history(baseline_path, failure_path)
        if [history["baseline_report_sha256"], history["prior_failure_report_sha256"]] != expected_hashes:
            raise ProbeStop("history_fingerprint_changed")
        if importlib.metadata.version("gallery-dl") != gallery.PINNED_GALLERY:
            raise ProbeStop("gallery_version_mismatch")
        deadline = time.monotonic() + WORKER_TIMEOUT
        jar, cookie_evidence = load_bili_cookies(profile, None)
        report["cookie_evidence"] = cookie_evidence
        client = BoundedClient(jar, max_requests=1)
        client.allowed_paths = {"/x/web-interface/nav"}
        account = run_account(client.get)
        report.update({k: account[k] for k in ("account", "nav_evidence", "stop_reason", "stop_evidence") if k in account})
        if report["account"] != "PASS" or "stop_reason" in report:
            return report
        transport = PageTransport(jar, deadline, account_requests=client.request_count, last_request_at=client.last_request_at)
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            report["baseline_checks"] = extract_metadata(transport, baseline)
        report["baseline"] = "MATCH"
    except ProbeStop as exc:
        report.update(stop_reason=exc.reason, stop_evidence=exc.evidence)
        if exc.reason in ("fixed_sample_contract_changed", "fixed_sample_baseline_mismatch"):
            report["baseline"] = "MISMATCH"
    except Exception:
        report.update(stop_reason="page_diagnostic_failed")
    finally:
        if transport is not None:
            report.update(network_requests=transport.count, source_requests=transport.page_requests)
            conclude(report, transport, history)
        elif client is not None:
            report["network_requests"] = client.request_count
    return report


def run_bounded(profile, baseline_path, failure_path, hashes):
    job, process = WindowsJob(), None
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--profile", profile,
                   "--baseline-report", str(baseline_path), "--prior-failure-report", str(failure_path),
                   "--output", "unused", "--history-hashes", *hashes]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(process)
        try:
            stdout, _ = process.communicate(input=START_TOKEN, timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            job.close()
            process.communicate(timeout=5)
            return {"diagnosis": "INCOMPLETE", "stop_reason": "worker_total_timeout", "network_requests": None,
                    "request_count_status": "UNKNOWN_AFTER_TIMEOUT", "media_requests": 0, "total_media_bytes": 0}
        if process.returncode != 0 or len(stdout) > 128 * 1024:
            raise ProbeStop("invalid_worker_result")
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
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--prior-failure-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--history-hashes", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if sys.stdin.readline() != START_TOKEN:
            return 2
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = run_worker(args.profile, args.baseline_report, args.prior_failure_report, args.history_hashes)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    try:
        _, history = load_history(args.baseline_report, args.prior_failure_report)
        output = args.output.resolve()
        base = (ROOT / ".artifacts").resolve()
        if not base.is_relative_to(ROOT.resolve()) or output.parent != base or args.output.is_symlink():
            raise ProbeStop("evidence_outside_artifacts")
        handle = output.open("x", encoding="utf-8")
    except (ProbeStop, OSError, ValueError):
        parser.error("Requires valid historical reports and a new output directly inside .artifacts")
    report = {"schema_version": 1, "experiment": "U03_GALLERY_PAGE_STRUCTURE", "sample_dynamic_id": SAMPLE,
              "timestamp_utc": datetime.now(timezone.utc).isoformat(), "research_gate": "NEEDS_MORE_EVIDENCE",
              "gallery_dl_required": gallery.PINNED_GALLERY, "request_limit": REQUEST_LIMIT,
              "worker_timeout_seconds": WORKER_TIMEOUT, "media_requests": 0, "total_media_bytes": 0,
              "download": "NOT_RUN", "diagnosis": "IN_PROGRESS", **history}
    with handle:
        json.dump(report, handle)
        handle.flush()
        try:
            report.update(run_bounded(args.profile, args.baseline_report, args.prior_failure_report,
                                     [history["baseline_report_sha256"], history["prior_failure_report_sha256"]]))
        except Exception:
            report.update(diagnosis="INCOMPLETE", stop_reason="parent_diagnostic_failed", network_requests=None)
        handle.seek(0)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.truncate()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["diagnosis"] == "COMPLETE" and "stop_reason" not in report else 2


if __name__ == "__main__":
    raise SystemExit(main())
