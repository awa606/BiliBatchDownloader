import gzip
import hashlib
import http.cookiejar
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "experiments"))
import gallery_page_probe as probe
import test_gallery_download_probe as fixtures

PRIVATE = fixtures.PRIVATE


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline test attempted credentials or network")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", blocked)
    monkeypatch.setattr(probe, "load_bili_cookies", blocked)


def page_with(*values, extra=None, outside=""):
    page = fixtures.synthetic_page().decode()
    state = json.loads(page.split(probe.ASSIGNMENT)[1].split(";</script>")[0])
    for value in values:
        state["detail"]["modules"].append({"module_type": "MODULE_BLOCKED", "module_blocked": value})
    if extra:
        state.update(extra)
    return ("<html>" + outside + "<script>" + probe.ASSIGNMENT + json.dumps(state) + ";</script></html>").encode()


def transport(monkeypatch, body, headers=None):
    instance = probe.PageTransport(http.cookiejar.CookieJar(), time.monotonic() + 60)
    calls = []
    def fake_open(request, timeout):
        calls.append(request)
        assert timeout <= 15
        if isinstance(body, Exception):
            raise body
        return fixtures.Reply(body, headers=headers)
    monkeypatch.setattr(instance.page_opener, "open", fake_open)
    monkeypatch.setattr(probe.gallery.time, "sleep", lambda _: None)
    return instance, calls


def history_files(tmp_path, page):
    base = fixtures.write_baseline(tmp_path)
    data = json.loads(base.read_text())
    digest = hashlib.sha256(page).hexdigest()
    data["response_sha256"] = digest
    base.write_text(json.dumps(data))
    base_sha = hashlib.sha256(base.read_bytes()).hexdigest()
    failure = tmp_path / "failure.json"
    failure.write_text(json.dumps({"experiment": "U03_GALLERY_DOWNLOAD", "sample_dynamic_id": probe.SAMPLE,
        "stop_reason": "blocked_gallery_module", "network_requests": 2, "media_requests": 0,
        "total_media_bytes": 0, "assets": [], "baseline_report_sha256": base_sha,
        "source_response_sha256": digest, "source_metadata_decoding": {
            "status": "PASS_THROUGH", "raw_sha256": digest, "decoded_sha256": digest}}))
    return base, failure, [base_sha, hashlib.sha256(failure.read_bytes()).hexdigest()]


def worker(monkeypatch, tmp_path, page, *, account=None, headers=None, source_error=None, history_page=None):
    base, failure, hashes = history_files(tmp_path, history_page or page)
    nav = json.dumps(account or {"code": 0, "data": {"isLogin": True, "mid": 123}}).encode()
    replies = [fixtures.Reply(nav), source_error or fixtures.Reply(page, headers=headers)]
    calls = []
    def fake_open(self, request, timeout):
        calls.append(request)
        assert 0 < timeout <= 15
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    monkeypatch.setattr(probe, "load_bili_cookies", lambda *a: (http.cookiejar.CookieJar(), {}))
    monkeypatch.setattr(probe.gallery.time, "sleep", lambda _: None)
    report = probe.run_worker(PRIVATE, base, failure, hashes)
    assert PRIVATE not in json.dumps(report) and "https://" not in json.dumps(report)
    assert report["media_requests"] == 0 and report["total_media_bytes"] == 0
    return report, calls


@pytest.mark.parametrize("value,kind,truthy", [(None, "NULL", False), (False, "BOOLEAN", False),
    ({}, "OBJECT", False), ([], "ARRAY", False), ("", "STRING", False), (0, "NUMBER", False),
    ({"hint_message": PRIVATE}, "OBJECT", True)])
def test_real_upstream_original_and_flattened_types(monkeypatch, value, kind, truthy, capsys, caplog):
    instance, calls = transport(monkeypatch, page_with(value))
    checks = probe.extract_metadata(instance, fixtures.baseline())
    assert checks["image_count"] == 4 and len(calls) == 1
    field = instance.original["blocked_fields"][0]
    assert field == {"module_index": 2, "key_present": True, "value_type": kind, "truthy": truthy}
    assert instance.flattened["matches_last_original_field"]
    assert instance.state_audit["key_occurrences"][0]["location"] == "MODULE_FIELD"
    assert PRIVATE not in json.dumps(instance.state_audit) + capsys.readouterr().out + caplog.text


def test_observes_truthy_module_before_later_empty_field_overwrites(monkeypatch):
    instance, _ = transport(monkeypatch, page_with({"hint_message": PRIVATE}, None))
    probe.extract_metadata(instance, fixtures.baseline())
    assert instance.original["merge_overwrite_count"] == 1
    assert instance.original["blocked_fields"][0]["truthy"] is True
    assert instance.flattened["value_type"] == "NULL"
    report = {"blocking_module": "NOT_OBSERVED"}
    probe.conclude(report, instance, {"historical_raw_sha256": instance.page_sha256,
        "historical_decoded_sha256": instance.page_sha256})
    assert report["blocking_module"] == "PRESENT"
    assert report["historical_cause"] == "LITERAL_SCAN_WITH_EFFECTIVE_MODULE"


@pytest.mark.parametrize("scenario", ["outside", "other_key", "string_value", "escaped_key", "duplicate"])
def test_locations_and_duplicate_members(scenario):
    if scenario == "outside":
        page = page_with(outside='<script>var x="module_blocked";</script>')
    elif scenario == "other_key":
        page = page_with(extra={"module_blocked": PRIVATE})
    elif scenario == "string_value":
        page = page_with(extra={"note": "module_blocked"})
    elif scenario == "escaped_key":
        page = page_with(None).replace(b'"module_blocked"', b'"module_\\u0062locked"')
    else:
        page = page_with(None).replace(b'"module_blocked": null', b'"module_blocked": true, "module_blocked": null')
    result = probe.audit_state(page.decode())
    assert result["status"] == "PARSED" and PRIVATE not in json.dumps(result)
    if scenario == "outside":
        assert result["outside_selected_state_literal_count"] == 1 and result["key_occurrences"] == []
    elif scenario == "other_key":
        assert result["key_occurrences"][0]["location"] == "STATE_OTHER_FIELD"
    elif scenario == "string_value":
        assert result["string_value_marker_count"] == 1 and result["key_occurrences"] == []
    elif scenario == "escaped_key":
        assert result["whole_page_literal_count"] == 0 and len(result["key_occurrences"]) == 1
    else:
        assert result["duplicate_blocked_member_count"] == 1 and len(result["key_occurrences"]) == 2


@pytest.mark.parametrize("body", ["", "window.__INITIAL_STATE__={bad};", "window.__INITIAL_STATE__={}",
    'window.__INITIAL_STATE__={"note":"cut};inside"};'])
def test_parser_failure_does_not_guess_other_state(body):
    assert probe.audit_state(body)["status"] == "PARSE_FAILED"


def test_multiple_assignments_follow_exact_upstream_selection():
    first = page_with(None).decode()
    second = page_with({"hint_message": PRIVATE}).decode()
    result = probe.audit_state(first + second)
    assert result["assignment_count"] == 2
    assert result["key_occurrences"][0]["value_type"] == "NULL"
    assert result["outside_selected_state_literal_count"] == 1


def test_diagnostic_depth_is_bounded():
    state = {"x": None}
    for _ in range(65):
        state = {"x": state}
    assert probe.audit_state(probe.ASSIGNMENT + json.dumps(state) + ";")["status"] == "DIAGNOSTIC_LIMIT"


def test_third_physical_request_is_impossible(monkeypatch):
    instance, calls = transport(monkeypatch, page_with(None))
    instance.fetch_page(instance.page)
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        instance.open(instance.page, media=False)
    assert len(calls) == 1 and instance.count == 2
    with pytest.raises(probe.ProbeStop, match="source_request_budget_exhausted"):
        instance.fetch_page(instance.page)


@pytest.mark.parametrize("action", ["authorize", "download", "direct_media", "foreign_page"])
def test_all_media_and_other_endpoint_entries_forbidden(monkeypatch, action):
    instance, calls = transport(monkeypatch, page_with(None))
    with pytest.raises(probe.ProbeStop):
        if action == "authorize":
            instance.allow_assets(fixtures.assets())
        elif action == "download":
            instance.download({}, Path("never.mp4"))
        else:
            instance.open("https://i0.hdslb.com/never.png", media=action == "direct_media")
    assert calls == [] and instance.count == 1


@pytest.mark.parametrize("status", [301, 412, 429])
def test_http_stops_without_retry_or_location_follow(monkeypatch, tmp_path, status):
    error = urllib.error.HTTPError("https://private.test/" + PRIVATE, status, PRIVATE,
                                   {"Location": "https://private.test/" + PRIVATE}, None)
    report, calls = worker(monkeypatch, tmp_path, page_with(None), source_error=error)
    assert len(calls) == 2 and report["stop_evidence"] == {"http_status": status}
    assert report["diagnosis"] == "INCOMPLETE"


@pytest.mark.parametrize("marker", [b"window._riskdata_", b"window.__riskdata__"])
@pytest.mark.parametrize("compressed", [False, True])
def test_risk_page_stops_before_extraction(monkeypatch, tmp_path, marker, compressed):
    page = page_with(None) + marker
    report, calls = worker(monkeypatch, tmp_path, gzip.compress(page) if compressed else page,
                          headers={"Content-Encoding": "gzip"} if compressed else {})
    assert report["stop_reason"] == "verification_page_detected" and len(calls) == 2
    assert report["source_structure"] is None


@pytest.mark.parametrize("account", [{"code": -101, "data": {}}, {"code": 0, "data": {"isLogin": False}}])
def test_login_failure_prevents_source(monkeypatch, tmp_path, account):
    report, calls = worker(monkeypatch, tmp_path, page_with(None), account=account)
    assert report["account"] == "BLOCKED" and len(calls) == 1 and report["network_requests"] == 1


@pytest.mark.parametrize("scenario", ["gzip", "truncated", "unknown_encoding", "expanded_limit", "raw_limit", "timeout"])
def test_shared_transport_decoding_and_limits(monkeypatch, tmp_path, scenario):
    page, headers, error = page_with(None), {}, None
    if scenario == "timeout":
        error = TimeoutError(PRIVATE)
    elif scenario == "raw_limit":
        page = b"x" * (probe.gallery.MAX_PAGE_BYTES + 1)
    elif scenario == "unknown_encoding":
        headers = {"Content-Encoding": "br"}
    else:
        page = gzip.compress(b"x" * (probe.gallery.MAX_PAGE_BYTES + 1) if scenario == "expanded_limit" else page)
        headers = {"Content-Encoding": "GZip"}
        if scenario == "truncated":
            page = page[:-2]
    report, calls = worker(monkeypatch, tmp_path, page, headers=headers, source_error=error)
    assert len(calls) == 2
    if scenario == "gzip":
        assert report["diagnosis"] == "COMPLETE" and report["baseline"] == "MATCH"
    else:
        assert report["diagnosis"] == "INCOMPLETE" and "stop_reason" in report


def test_success_diagnoses_false_positive_but_never_releases_download(monkeypatch, tmp_path):
    report, calls = worker(monkeypatch, tmp_path, page_with(None))
    assert len(calls) == 2 and report["network_requests"] == 2
    assert report["diagnosis"] == "COMPLETE" and report["baseline"] == "MATCH"
    assert report["blocking_module"] == "NO_EFFECTIVE_MODULE"
    assert report["historical_cause"] == "LITERAL_SCAN_WITHOUT_EFFECTIVE_MODULE"
    assert report["download"] == "NOT_RUN"


def test_changed_page_cannot_explain_historical_failure(monkeypatch, tmp_path):
    report, _ = worker(monkeypatch, tmp_path, page_with(None), history_page=page_with(False))
    assert report["diagnosis"] == "COMPLETE"
    assert report["historical_cause"] == "UNRESOLVED" and not report["historical_raw_matches"]


def test_author_mismatch_does_not_discard_module_diagnosis(monkeypatch, tmp_path):
    page = page_with(None).replace(probe.gallery.AUTHOR_UID.encode(), b"999")
    report, _ = worker(monkeypatch, tmp_path, page)
    assert report["diagnosis"] == "COMPLETE" and report["baseline"] == "MISMATCH"


@pytest.mark.parametrize("value", [True, "private", 1])
def test_nonobject_truthy_module_does_not_get_false_clear(monkeypatch, tmp_path, value):
    report, _ = worker(monkeypatch, tmp_path, page_with(value))
    assert report["diagnosis"] == "INCOMPLETE" and report["blocking_module"] == "UNRESOLVED_TYPE"
    assert report["historical_cause"] == "UNRESOLVED"


def test_changed_history_stops_before_cookies(monkeypatch, tmp_path):
    base, failure, _ = history_files(tmp_path, page_with(None))
    report = probe.run_worker(PRIVATE, base, failure, ["0" * 64, "0" * 64])
    assert report["stop_reason"] == "history_fingerprint_changed" and report["network_requests"] == 0


def test_dependency_drift_stops_before_cookies(monkeypatch, tmp_path):
    base, failure, hashes = history_files(tmp_path, page_with(None))
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _: "unexpected")
    report = probe.run_worker(PRIVATE, base, failure, hashes)
    assert report["stop_reason"] == "gallery_version_mismatch" and report["network_requests"] == 0


def test_deadline_expired_before_page_sends_nothing(monkeypatch):
    instance, calls = transport(monkeypatch, page_with(None))
    instance.deadline = time.monotonic() - 1
    with pytest.raises(probe.ProbeStop, match="worker_total_timeout"):
        instance.fetch_page(instance.page)
    assert calls == [] and instance.count == 1


def test_inherited_gap_headers_and_no_redirect_handler(monkeypatch):
    instance, calls = transport(monkeypatch, page_with(None))
    sleeps = []
    instance.last_request_at = 10
    instance.deadline = 100
    monkeypatch.setattr(probe.gallery.time, "monotonic", lambda: 10.25)
    monkeypatch.setattr(probe.gallery.time, "sleep", sleeps.append)
    instance.fetch_page(instance.page)
    assert sleeps == [0.75]
    assert dict(calls[0].header_items()) == {"User-agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/", "Accept-encoding": "identity"}
    assert any(isinstance(h, probe.gallery.NoRedirect) for h in instance.page_opener.handlers)


@pytest.mark.parametrize("edit", ["order", "missing", "path"])
def test_asset_baseline_changes_never_get_match(monkeypatch, tmp_path, edit):
    page = page_with(None)
    if edit == "order":
        page = page.replace(b"image1.png", b"imageZ.png").replace(b"image2.png", b"image1.png").replace(b"imageZ.png", b"image2.png")
    elif edit == "missing":
        state = json.loads(page.decode().split(probe.ASSIGNMENT)[1].split(";</script>")[0])
        state["detail"]["modules"][1]["module_top"]["display"]["album"]["pics"].pop()
        page = (probe.ASSIGNMENT + json.dumps(state) + ";").encode()
    else:
        page = page.replace(b"image1.png", b"different.png")
    report, _ = worker(monkeypatch, tmp_path, page)
    assert report["baseline"] == "MISMATCH" and report["download"] == "NOT_RUN"


def test_invalid_structure_and_parser_error_keep_diagnostic_evidence(monkeypatch, tmp_path):
    report, calls = worker(monkeypatch, tmp_path, b'window.__INITIAL_STATE__={invalid "module_blocked"};')
    assert len(calls) == 2 and report["source_structure"]["status"] == "PARSE_FAILED"
    assert report["diagnosis"] == "INCOMPLETE" and report["historical_cause"] == "UNRESOLVED"


def test_invalid_history_stops_before_cookies(monkeypatch, tmp_path):
    base, failure, hashes = history_files(tmp_path, page_with(None))
    failure.write_text(json.dumps({"secret": PRIVATE}))
    report = probe.run_worker(PRIVATE, base, failure, hashes)
    assert report["network_requests"] == 0 and PRIVATE not in json.dumps(report)


def test_parent_reserves_output_and_never_overwrites(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    directory = tmp_path / ".artifacts"
    directory.mkdir()
    base, failure, _ = history_files(tmp_path, page_with(None))
    target = directory / "diagnosis.json"
    def bounded(*args):
        assert json.loads(target.read_text())["diagnosis"] == "IN_PROGRESS"
        return {"diagnosis": "COMPLETE", "network_requests": 2}
    monkeypatch.setattr(probe, "run_bounded", bounded)
    args = ["--profile", PRIVATE, "--baseline-report", str(base), "--prior-failure-report", str(failure), "--output", str(target)]
    assert probe.main(args) == 0
    before = target.read_bytes()
    with pytest.raises(SystemExit):
        probe.main(args)
    assert target.read_bytes() == before and PRIVATE not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        probe.main(args[:-1] + [str(tmp_path / "outside.json")])


@pytest.mark.parametrize("scenario", ["success", "timeout", "assignment_failure", "launch_failure"])
def test_job_lifecycle_and_unknown_count_after_timeout(monkeypatch, scenario):
    events = []
    class Job:
        def assign(self, process):
            events.append("assigned")
            if scenario == "assignment_failure":
                raise probe.ProbeStop("job_assignment_failed")
        def close(self):
            events.append("closed")
    class Process:
        returncode = None
        count = 0
        def communicate(self, input=None, timeout=None):
            self.count += 1
            if input:
                assert "assigned" in events and input == probe.START_TOKEN and timeout == 60
                events.append("start")
            if scenario == "timeout" and self.count == 1:
                raise subprocess.TimeoutExpired(PRIVATE, timeout)
            self.returncode = 0
            return json.dumps({"diagnosis": "COMPLETE"}), ""
        def poll(self):
            return self.returncode
        def kill(self):
            events.append("killed")
        def wait(self, timeout):
            self.returncode = 1
    def launch(*a, **kw):
        if scenario == "launch_failure":
            raise OSError(PRIVATE)
        return Process()
    monkeypatch.setattr(probe, "WindowsJob", Job)
    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    if scenario in ("assignment_failure", "launch_failure"):
        with pytest.raises(Exception):
            probe.run_bounded(PRIVATE, Path("baseline"), Path("failure"), ["a", "b"])
        assert "start" not in events
    else:
        result = probe.run_bounded(PRIVATE, Path("baseline"), Path("failure"), ["a", "b"])
        if scenario == "timeout":
            assert result["network_requests"] is None and result["media_requests"] == 0
            assert result["stop_reason"] == "worker_total_timeout"
    assert "closed" in events


def test_worker_handshake_before_work(monkeypatch):
    monkeypatch.setattr(probe.sys, "stdin", io.StringIO("wrong\n"))
    monkeypatch.setattr(probe, "run_worker", lambda *args: pytest.fail("Started without handshake"))
    assert probe.main(["--worker", "--profile", PRIVATE, "--baseline-report", "x", "--prior-failure-report", "y", "--output", "z"]) == 2
