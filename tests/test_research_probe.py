import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("research_probe", Path(__file__).parents[1] / "experiments" / "research_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize("status,body,reason", [
    (412, b"private server response", "http_stop"),
    (200, b"<html>verification</html>", "non_json_or_verification_page"),
    (200, b'{"code":-101}', "login_or_platform_stop"),
    (200, b'{"code":-352}', "login_or_platform_stop"),
    (200, b'{"code":0}', "missing_data_contract"),
    (200, b'{"data":{}}', "missing_response_contract"),
])
def test_stop_conditions_do_not_echo_body(status, body, reason):
    with pytest.raises(probe.ProbeStop) as caught:
        probe.classify_response(status, body)
    assert caught.value.reason == reason
    assert body.decode() not in json.dumps(caught.value.evidence)


def nav(logged_in=True):
    return {"data": {"isLogin": logged_in, "mid": 123}, "evidence": {"api_code": 0}}


def test_logged_out_never_requests_followings():
    calls = []
    result = probe.run_account(lambda *a: calls.append(a) or nav(False), followings=True)
    assert result["account"] == "BLOCKED"
    assert len(calls) == 1


def test_public_endpoint_success_not_proof_of_login():
    result = probe.run_account(lambda *a: {"data": {"mid": 123}, "evidence": {}})
    assert result["account"] == "BLOCKED"


def test_partial_followings_preserve_counts_and_stop():
    calls = []
    def get(path, params=None):
        calls.append(path)
        if params is None:
            return nav()
        if params["pn"] == 1:
            return {"data": {"list": [{"mid": i} for i in range(1, 51)], "total": 100}, "evidence": {}}
        raise probe.ProbeStop("login_or_platform_stop", {"api_code": -352})
    result = probe.run_account(get, followings=True)
    assert result["followings"] == "PARTIAL"
    assert result["unique_count"] == 50
    assert len(calls) == 3
    assert "mid" not in json.dumps(result)


def test_complete_small_list():
    def get(path, params=None):
        return nav() if params is None else {"data": {"list": [{"mid": 7}], "total": 1}, "evidence": {}}
    result = probe.run_account(get, followings=True)
    assert result["followings"] == "PASS"
    assert result["scope"] == "complete"


def test_large_list_is_bounded_and_not_falsely_complete():
    pages = []
    def get(path, params=None):
        if params is None:
            return nav()
        page = params["pn"]
        pages.append(page)
        return {"data": {"list": [{"mid": i} for i in range(page*50, page*50+50)], "total": 800}, "evidence": {}}
    result = probe.run_account(get, followings=True)
    assert pages == [1, 2, 5, 6]
    assert result["followings"] == "PARTIAL"
    assert result["unique_count"] == 200


def test_failed_evidence_cannot_be_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    target = tmp_path / ".artifacts" / "result.json"
    probe.save_exclusive({"status": "FAIL"}, target)
    with pytest.raises(FileExistsError):
        probe.save_exclusive({"status": "PASS"}, target)
    assert json.loads(target.read_text())["status"] == "FAIL"


def test_report_must_stay_in_artifact_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        probe.save_exclusive({}, tmp_path / "public.json")


def test_redirect_is_stopped_without_followup_request():
    with pytest.raises(probe.ProbeStop, match="redirect_blocked"):
        probe.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test/")


def test_bad_output_stops_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    def forbidden(*args):
        pytest.fail("Credentials must not be read")
    monkeypatch.setattr(probe, "load_bili_cookies", forbidden)
    with pytest.raises(SystemExit) as caught:
        probe.main(["--profile", "synthetic-profile", "--output", str(tmp_path / "wrong.json")])
    assert caught.value.code == 2


def test_partial_followings_get_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "load_bili_cookies", lambda *a: (None, {}))
    monkeypatch.setattr(probe, "run_account", lambda *a, **k: {"account": "PASS", "followings": "PARTIAL"})
    target = tmp_path / ".artifacts" / "partial.json"
    assert probe.main(["--profile", "synthetic-profile", "--followings", "--output", str(target)]) == 2
    assert json.loads(target.read_text())["followings"] == "PARTIAL"


def test_existing_evidence_stops_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    target = tmp_path / ".artifacts" / "old.json"
    probe.save_exclusive({"status": "FAIL"}, target)
    monkeypatch.setattr(probe, "load_bili_cookies", lambda *a: pytest.fail("Unexpected credential read"))
    with pytest.raises(SystemExit):
        probe.main(["--profile", "synthetic-profile", "--output", str(target)])
    assert json.loads(target.read_text())["status"] == "FAIL"


def test_probe_failure_records_only_exception_type(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    def failure(*args):
        raise RuntimeError("SYNTHETIC_PRIVATE_MESSAGE")
    monkeypatch.setattr(probe, "load_bili_cookies", failure)
    target = tmp_path / ".artifacts" / "unexpected.json"
    assert probe.main(["--profile", "synthetic-profile", "--output", str(target)]) == 2
    assert "SYNTHETIC_PRIVATE_MESSAGE" not in target.read_text()


def test_full_bounded_traversal_checks_every_page():
    pages = []
    def get(path, params=None):
        if params is None:
            return nav()
        page = params["pn"]
        pages.append(page)
        start = (page - 1) * 50 + 1
        return {"data": {"list": [{"mid": i} for i in range(start, min(start+50, 387))], "total": 386}, "evidence": {}}
    result = probe.run_account(get, followings=True, full_pages=8)
    assert pages == list(range(1, 9))
    assert result["unique_count"] == 386
    assert result["followings"] == "PASS"


def test_full_traversal_cap_still_reports_partial():
    def get(path, params=None):
        if params is None:
            return nav()
        start = (params["pn"] - 1) * 50 + 1
        return {"data": {"list": [{"mid": i} for i in range(start, start+50)], "total": 500}, "evidence": {}}
    assert probe.run_account(get, followings=True, full_pages=2)["followings"] == "PARTIAL"
