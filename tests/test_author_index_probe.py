import http.cookiejar
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "experiments"))
import author_index_probe as probe
import research_probe as common

UID = "1337856191"


def item(identifier, kind="DYNAMIC_TYPE_DRAW", uid=UID):
    return {"id_str": identifier, "type": kind,
            "modules": {"module_author": {"mid": int(uid), "name": "SYNTHETIC_PRIVATE"},
                        "module_dynamic": {"desc": {"text": "SYNTHETIC_PRIVATE"}}}}


def run_pages(pages, *, logged_in=True):
    calls = []
    def get(path, params=None):
        calls.append((path, params))
        if path == "/x/web-interface/nav":
            return {"data": {"isLogin": logged_in, "mid": 1}, "evidence": {"api_code": 0}}
        value = pages[len(calls) - 2]
        if isinstance(value, Exception):
            raise value
        return {"data": value, "evidence": {"http_status": 200, "api_code": 0}}
    return probe.run_index(get, UID), calls


def page(items, more=False, offset=""):
    return {"items": items, "has_more": more, "offset": offset}


def test_complete_requires_terminal_page_and_unique_identities():
    result, calls = run_pages([page([item("10")], True, "cursor-A"), page([item("11", "DYNAMIC_TYPE_AV")])])
    assert result["status"] == "COMPLETE"
    assert result["unique_count"] == 2
    assert result["type_counts"] == {"DYNAMIC_TYPE_DRAW": 1, "DYNAMIC_TYPE_AV": 1}
    assert calls[2][1] == {"host_mid": UID, "offset": "cursor-A", "platform": "web"}
    assert "cursor-A" not in json.dumps(result)


def test_two_pages_with_more_never_claim_complete():
    result, calls = run_pages([page([item("10")], True, "A"), page([item("11")], True, "B")])
    assert len(calls) == 3
    assert result["status"] == "PARTIAL"
    assert result["scope"] == "first_two_pages_only"


def test_login_failure_prevents_index_request():
    result, calls = run_pages([], logged_in=False)
    assert len(calls) == 1
    assert result["status"] == "BLOCKED"


def test_platform_failure_stops_and_preserves_first_page():
    result, calls = run_pages([page([item("10")], True, "A"),
                               probe.ProbeStop("login_or_platform_stop", {"api_code": -352})])
    assert len(calls) == 3
    assert result["status"] == "PARTIAL"
    assert result["unique_count"] == 1
    assert result["stop_evidence"] == {"api_code": -352}


@pytest.mark.parametrize("second", ["A", ""])
def test_offset_stalls_never_claim_complete(second):
    result, calls = run_pages([page([item("10")], True, "A"), page([item("11")], True, second)])
    assert len(calls) == 3
    assert result["status"] == "PARTIAL"
    assert result["stop_reason"] in {"offset_did_not_advance", "missing_next_offset"}


def test_duplicate_even_on_terminal_page_is_partial():
    result, _ = run_pages([page([item("10")], True, "A"), page([item("10")])])
    assert result["status"] == "PARTIAL"
    assert result["unique_count"] == 1
    assert result["duplicate_count"] == 1


@pytest.mark.parametrize("bad_page,reason", [
    ({"items": [], "has_more": 0}, "missing_index_page_contract"),
    (page([], True, "A"), "empty_page_with_more"),
    (page([item("10")], True, None), "missing_next_offset"),
    (page([item("10", uid="6031707")]), "unexpected_publisher_identity"),
    (page([item("SYNTHETIC_PRIVATE")]), "missing_dynamic_identity"),
])
def test_invalid_contracts_stop_without_followup(bad_page, reason):
    result, calls = run_pages([bad_page])
    assert len(calls) == 2
    assert result["status"] != "COMPLETE"
    assert result["stop_reason"] == reason
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)


def test_repost_preserves_original_identity_without_names_or_body():
    forwarded = item("10", "DYNAMIC_TYPE_FORWARD")
    forwarded["orig"] = item("11", "DYNAMIC_TYPE_AV", "6031707")
    result, _ = run_pages([page([forwarded])])
    assert result["status"] == "COMPLETE"
    summary = result["items"][0]
    assert summary["publisher_uid"] == UID
    assert summary["original"]["publisher_uid"] == "6031707"
    assert summary["original"]["dynamic_id"] == "11"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)


def test_missing_original_identity_cannot_look_complete():
    result, _ = run_pages([page([item("10", "DYNAMIC_TYPE_FORWARD")])])
    assert result["status"] == "BLOCKED"
    assert result["stop_reason"] == "missing_original_identity"


def test_client_has_two_endpoints_and_three_request_budget(monkeypatch):
    client = probe.AuthorIndexClient(http.cookiejar.CookieJar())
    assert client.allowed_paths == {"/x/web-interface/nav", probe.ENDPOINT}
    assert client.max_requests == 3
    monkeypatch.setattr(client.opener, "open", lambda *a, **k: pytest.fail("Unexpected network"))
    with pytest.raises(probe.ProbeStop, match="endpoint_not_allowed"):
        client.get("/x/relation/followings")
    client.request_count = 3
    with pytest.raises(probe.ProbeStop, match="request_budget_exhausted"):
        client.get(probe.ENDPOINT)


def test_empty_terminal_list_is_complete_without_more_requests():
    result, calls = run_pages([page([])])
    assert result["status"] == "COMPLETE"
    assert result["unique_count"] == 0
    assert len(calls) == 2


def test_bad_output_and_unknown_author_never_read_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "load_bili_cookies", lambda *a: pytest.fail("Credentials read"))
    for uid, output in ((UID, tmp_path / "outside.json"), ("999", tmp_path / ".artifacts" / "new.json")):
        with pytest.raises(SystemExit):
            probe.main(["--profile", "synthetic", "--uid", uid, "--output", str(output)])


def test_existing_evidence_is_preserved_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    target = tmp_path / ".artifacts" / "old.json"
    common.save_exclusive({"status": "FAIL"}, target)
    monkeypatch.setattr(probe, "load_bili_cookies", lambda *a: pytest.fail("Credentials read"))
    with pytest.raises(SystemExit):
        probe.main(["--profile", "synthetic", "--uid", UID, "--output", str(target)])
    assert json.loads(target.read_text())["status"] == "FAIL"


@pytest.mark.parametrize("status,expected_exit", [("PARTIAL", 2), ("COMPLETE", 0)])
def test_cli_state_is_not_research_pass(tmp_path, monkeypatch, status, expected_exit):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "load_bili_cookies", lambda *a: (http.cookiejar.CookieJar(), {}))
    monkeypatch.setattr(probe, "run_index", lambda *a: {"status": status})
    target = tmp_path / ".artifacts" / "result.json"
    assert probe.main(["--profile", "PRIVATE_PROFILE", "--uid", UID, "--output", str(target)]) == expected_exit
    report = json.loads(target.read_text())
    assert report["research_gate"] == "NEEDS_MORE_EVIDENCE"
    assert report["media_downloads"] == report["network_requests"] == 0
    assert "PRIVATE_PROFILE" not in target.read_text()


def test_exception_messages_are_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    def fail(*args):
        raise RuntimeError("SYNTHETIC_PRIVATE")
    monkeypatch.setattr(probe, "load_bili_cookies", fail)
    target = tmp_path / ".artifacts" / "failure.json"
    assert probe.main(["--profile", "PRIVATE_PROFILE", "--uid", UID, "--output", str(target)]) == 2
    assert "SYNTHETIC_PRIVATE" not in target.read_text()
