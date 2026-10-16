"""Two-page author dynamic-index diagnostic; never fetch media or claim Research PASS.

Request/response field evidence (not copied implementation): MediaCrawler
8f583999006d5b692272c608440e193ca282e73b, get_creator_dynamics and
get_creator_all_dynamics. That client additionally signs requests by default.
This isolated probe tests only authenticated base parameters, with no signature
or fingerprint fallback. A rejection ends the experiment.

Prepared offline on 2026-10-16 after the video probe received HTTP 412.
Live validation of this probe: NOT_RUN_PLATFORM_STOP. No live run is authorized
by this source file; the parent experiment must review its stop condition first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from research_probe import BoundedClient, ProbeStop, load_bili_cookies, open_evidence, run_account

AUTHORS = frozenset({"1337856191", "6031707"})
ENDPOINT = "/x/polymer/web-dynamic/v1/feed/space"
PAGE_LIMIT = 2
SOURCE = ("https://github.com/NanmiCoder/MediaCrawler/blob/"
          "8f583999006d5b692272c608440e193ca282e73b/media_platform/bilibili/client.py")


class AuthorIndexClient(BoundedClient):
    allowed_paths = {"/x/web-interface/nav", ENDPOINT}

    def __init__(self, jar):
        super().__init__(jar, max_requests=1 + PAGE_LIMIT)


def identity(value, reason):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProbeStop(reason)
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,29}", result):
        raise ProbeStop(reason)
    return result


def item_identity(item):
    if not isinstance(item, dict):
        raise ProbeStop("missing_dynamic_contract")
    identifier = identity(item.get("id_str"), "missing_dynamic_identity")
    kind = item.get("type")
    if not isinstance(kind, str) or not re.fullmatch(r"DYNAMIC_TYPE_[A-Z0-9_]{1,64}", kind):
        raise ProbeStop("missing_dynamic_type")
    modules = item.get("modules")
    author = modules.get("module_author") if isinstance(modules, dict) else None
    uid = identity(author.get("mid") if isinstance(author, dict) else None,
                   "missing_publisher_identity")
    return {"dynamic_id": identifier, "dynamic_type": kind, "publisher_uid": uid}


def summarize_item(item, requested_uid):
    summary = item_identity(item)
    if summary["publisher_uid"] != requested_uid:
        raise ProbeStop("unexpected_publisher_identity")
    original = item.get("orig")
    if original is not None:
        summary["original"] = item_identity(original)
    elif summary["dynamic_type"] == "DYNAMIC_TYPE_FORWARD":
        raise ProbeStop("missing_original_identity")
    return summary


def run_index(get, author_uid):
    if author_uid not in AUTHORS:
        raise ProbeStop("author_not_in_fixed_samples")
    report = {"account": "NOT_TESTED", "status": "NOT_TESTED", "scope": "not_started",
              "pages": [], "items": [], "unique_count": 0, "duplicate_count": 0,
              "type_counts": {}}
    try:
        account = run_account(get)
        report["account"] = account["account"]
        if "nav_evidence" in account:
            report["nav_evidence"] = account["nav_evidence"]
        if account["account"] != "PASS" or "stop_reason" in account:
            raise ProbeStop(account.get("stop_reason", "account_failed"), account.get("stop_evidence", {}))
        offset = ""
        seen_offsets = {offset}
        seen_ids = set()
        for page_number in range(1, PAGE_LIMIT + 1):
            response = get(ENDPOINT, {"host_mid": author_uid, "offset": offset, "platform": "web"})
            data = response["data"]
            rows, has_more = data.get("items"), data.get("has_more")
            if not isinstance(rows, list) or type(has_more) is not bool:
                raise ProbeStop("missing_index_page_contract", response["evidence"])
            page = {"page": page_number, "received_count": len(rows), "has_more": has_more,
                    "response_evidence": response["evidence"]}
            report["pages"].append(page)
            for item in rows:
                summary = summarize_item(item, author_uid)
                identifier = summary["dynamic_id"]
                if identifier in seen_ids:
                    report["duplicate_count"] += 1
                    continue
                seen_ids.add(identifier)
                report["items"].append(summary)
                kind = summary["dynamic_type"]
                report["type_counts"][kind] = report["type_counts"].get(kind, 0) + 1
                report["unique_count"] = len(seen_ids)
            if report["duplicate_count"]:
                raise ProbeStop("duplicate_dynamic_identity")
            if not has_more:
                report.update(status="COMPLETE", scope="complete_server_snapshot")
                return report
            if not rows:
                raise ProbeStop("empty_page_with_more")
            following_offset = data.get("offset")
            if not isinstance(following_offset, str) or not following_offset or len(following_offset) > 512:
                raise ProbeStop("missing_next_offset")
            page["next_offset_sha256"] = hashlib.sha256(following_offset.encode()).hexdigest()
            if following_offset in seen_offsets:
                raise ProbeStop("offset_did_not_advance")
            seen_offsets.add(following_offset)
            offset = following_offset
        report.update(status="PARTIAL", scope="first_two_pages_only", stop_reason="page_budget_reached")
    except ProbeStop as exc:
        report.update(status="PARTIAL" if report["items"] else "BLOCKED",
                      scope="incomplete", stop_reason=exc.reason, stop_evidence=exc.evidence)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Explicit dedicated Chrome profile; never saved")
    parser.add_argument("--uid", required=True, choices=sorted(AUTHORS))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        handle = open_evidence(args.output)
    except (ValueError, OSError):
        parser.error("Output must be a new writable file inside .artifacts; no requests sent")
    report = {"schema_version": 1, "experiment": "AUTHOR_DYNAMIC_INDEX",
              "timestamp_utc": datetime.now(timezone.utc).isoformat(), "author_uid": args.uid,
              "research_gate": "NEEDS_MORE_EVIDENCE", "status": "IN_PROGRESS",
              "media_downloads": 0, "network_requests": 0, "max_pages": PAGE_LIMIT,
              "source_contract": SOURCE, "request_variant": "authenticated_unsigned_base_parameters",
              "credential_values_persisted": False}
    client = None
    with handle:
        json.dump(report, handle)
        handle.flush()
        try:
            jar, _ = load_bili_cookies(args.profile, None)
            client = AuthorIndexClient(jar)
            report.update(run_index(client.get, args.uid))
        except ProbeStop as exc:
            report.update(status="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
        except Exception as exc:
            report.update(status="BLOCKED", stop_reason="unexpected_probe_error",
                          stop_evidence={"exception_type": type(exc).__name__})
        finally:
            report["network_requests"] = client.request_count if client else 0
        handle.seek(0)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.truncate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
