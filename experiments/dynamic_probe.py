"""Single-ID dynamic metadata investigation; no media downloads."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from research_probe import BoundedClient, ProbeStop, load_bili_cookies, open_evidence, run_account


class DynamicClient(BoundedClient):
    allowed_paths = {"/x/web-interface/nav", "/x/polymer/web-dynamic/v1/detail"}


def summarize(item, depth=0):
    if not isinstance(item, dict) or depth > 1:
        raise ProbeStop("missing_dynamic_contract")
    modules = item.get("modules", {})
    author = modules.get("module_author", {})
    uid = author.get("mid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise ProbeStop("missing_dynamic_author")
    dynamic = modules.get("module_dynamic", {})
    major = dynamic.get("major") or {}
    kind = major.get("type", "")
    result = {"dynamic_id": item.get("id_str"), "dynamic_type": item.get("type"),
              "publisher_uid": str(uid), "body_present": bool(dynamic.get("desc")),
              "major_type": kind, "images": []}
    pics = (major.get("draw") or {}).get("items", [])
    if kind == "MAJOR_TYPE_OPUS":
        pics = (major.get("opus") or {}).get("pics", [])
    for index, picture in enumerate(pics, 1):
        url = picture.get("src") or picture.get("url") or ""
        if not url:
            raise ProbeStop("missing_dynamic_image")
        path = urlsplit(url).path
        result["images"].append({"sequence": index, "format": Path(path).suffix.lstrip("."),
                                 "source_path_sha256": hashlib.sha256(path.encode()).hexdigest(),
                                 "live_video_present": bool(picture.get("live_url"))})
    archive = major.get("archive")
    if archive:
        result["video_bvid"] = archive.get("bvid")
    if item.get("orig"):
        result["original"] = summarize(item["orig"], depth + 1)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[1-9][0-9]{5,25}", args.id):
        parser.error("Invalid fixed dynamic ID")
    try:
        handle = open_evidence(args.output)
    except (ValueError, OSError):
        parser.error("Output must be a new writable file inside .artifacts")
    report = {"experiment": "U03_dynamic_detail_diagnostic", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "requested_id": args.id, "research_gate": "NEEDS_MORE_EVIDENCE", "media_downloads": 0,
              "status": "IN_PROGRESS"}
    client = None
    with handle:
        json.dump(report, handle)
        handle.flush()
        try:
            jar, _ = load_bili_cookies(args.profile, None)
            client = DynamicClient(jar, max_requests=2)
            account = run_account(client.get)
            if account.get("account") != "PASS":
                raise ProbeStop(account.get("stop_reason", "account_failed"), account.get("stop_evidence", {}))
            response = client.get("/x/polymer/web-dynamic/v1/detail", {"id": args.id})
            report["response_evidence"] = response["evidence"]
            report["dynamic"] = summarize(response["data"].get("item"))
            if report["dynamic"]["dynamic_id"] != args.id:
                raise ProbeStop("unexpected_dynamic_identity")
            report["status"] = "METADATA_EXTRACTED_NOT_MEDIA_VERIFIED"
        except ProbeStop as exc:
            report.update(status="BLOCKED", stop_reason=exc.reason, stop_evidence=exc.evidence)
        except Exception as exc:
            report.update(status="FAIL", stop_reason="diagnostic_error", exception_type=type(exc).__name__)
        finally:
            report["network_requests"] = client.request_count if client else 0
        handle.seek(0)
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.truncate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "METADATA_EXTRACTED_NOT_MEDIA_VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
