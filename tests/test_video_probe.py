import http.cookiejar
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location("video_probe", Path(__file__).parents[1] / "experiments" / "video_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
URL = "https://www.bilibili.com/video/BV13x41117TL"
BVID = "BV13x41117TL"


@pytest.mark.parametrize("url", ["http://www.bilibili.com/video/BV13x41117TL", URL + "?p=3",
                                    URL + "?cookie=SECRET", "https://example.test/video/BV13x41117TL",
                                    "https://www.bilibili.com@evil.test/video/BV13x41117TL", URL + "\n"])
def test_reject_noncanonical_input(url):
    with pytest.raises(ValueError):
        probe.validate_url(url)


def entry(part=None):
    return {"id": BVID + (f"_p{part}" if part else ""), "uploader_id": "123",
            "tags": ["test"], "description": "private omitted body", "thumbnail": "https://cdn.test/?token=SECRET",
            "formats": [{"url": "https://cdn.test/video?token=SECRET"}]}


def test_summary_keeps_identity_without_signed_urls_or_description():
    result = probe.summarize(entry(), {"pages": [{"page": 1, "cid": 456}]}, BVID)
    text = json.dumps(result)
    assert result["parts"][0]["cid"] == 456
    assert result["parts"][0]["author_uid"] == "123"
    assert "SECRET" not in text and "cdn.test" not in text and "omitted body" not in text


def test_multipart_is_explicitly_bounded():
    result = probe.summarize({"_type": "playlist", "entries": [entry(1), entry(2)]},
                             {"pages": [{"page": n, "cid": 100 + n} for n in range(1, 24)]}, BVID)
    assert result["part_count"] == 23
    assert result["sampled_part_count"] == 2
    assert result["metadata_scope"] == "first_two_parts_only"


def test_missing_cid_or_incomplete_selected_parts_cannot_pass():
    with pytest.raises(probe.ProbeStop, match="missing_page_identity"):
        probe.capture_pages({"data": [{"page": 1}]}, {})
    with pytest.raises(probe.ProbeStop, match="incomplete_selected_parts"):
        probe.summarize(entry(), {"pages": [{"page": 1, "cid": 1}, {"page": 2, "cid": 2}]}, BVID)


def test_page_identity_changes_stop():
    observed = {}
    probe.capture_pages({"data": [{"page": 1, "cid": 2}]}, observed)
    with pytest.raises(probe.ProbeStop, match="page_identity_changed"):
        probe.capture_pages({"data": [{"page": 1, "cid": 3}]}, observed)


@pytest.mark.parametrize("message,reason", [("login required SECRET", "login_stop"),
                                            ("HTTP 412 SECRET", "platform_stop"),
                                            ("socket timed out SECRET", "transport_timeout")])
def test_warnings_stop_without_leaking_message(message, reason):
    with pytest.raises(probe.ProbeStop) as caught:
        probe.StopLogger().warning(message)
    assert caught.value.reason == reason
    assert "SECRET" not in str(caught.value)


def test_invalid_account_never_starts_video_requests(monkeypatch):
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _: probe.PINNED_VERSION)
    monkeypatch.setattr(probe.common, "load_bili_cookies", lambda *a: (http.cookiejar.CookieJar(), {}))
    monkeypatch.setattr(probe.common, "run_account", lambda *a: {"account": "BLOCKED", "stop_reason": "not_logged_in"})
    monkeypatch.setattr(probe, "extract_metadata", lambda *a: pytest.fail("Video request started after failed account check"))
    result = probe.run_worker("synthetic", URL)
    assert result["metadata"] == "NOT_TESTED"


def test_wrong_version_does_not_read_credentials(monkeypatch):
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _: "wrong")
    monkeypatch.setattr(probe.common, "load_bili_cookies", lambda *a: pytest.fail("Credential read"))
    assert probe.run_worker("synthetic", URL)["stop_reason"] == "dependency_version_mismatch"


def test_total_timeout_kills_only_own_child(monkeypatch):
    class Child:
        killed = False
        calls = 0
        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                assert timeout == 120
                raise subprocess.TimeoutExpired("safe", timeout)
            return "", None
        def kill(self):
            self.killed = True
    child = Child()
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **k: child)
    assert probe.run_bounded("synthetic", URL)["stop_reason"] == "worker_total_timeout"
    assert child.killed


def test_bad_or_existing_output_never_starts_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.common, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "run_bounded", lambda *a: pytest.fail("Worker launched"))
    target = tmp_path / ".artifacts" / "old.json"
    probe.common.save_exclusive({"status": "FAIL"}, target)
    for output in (tmp_path / "outside.json", target):
        with pytest.raises(SystemExit):
            probe.main(["--profile", "synthetic", "--url", URL, "--output", str(output)])
    assert json.loads(target.read_text())["status"] == "FAIL"


def test_worker_launch_failure_does_not_disclose_profile_or_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.common, "ROOT", tmp_path)
    def fail(*args):
        raise RuntimeError("SECRET_PRIVATE_PATH")
    monkeypatch.setattr(probe, "run_bounded", fail)
    target = tmp_path / ".artifacts" / "failure.json"
    assert probe.main(["--profile", "PRIVATE_PROFILE", "--url", URL, "--output", str(target)]) == 2
    text = target.read_text()
    assert "SECRET" not in text and "PRIVATE_PROFILE" not in text
    assert json.loads(text)["research_gate"] == "NEEDS_MORE_EVIDENCE"


def test_metadata_success_still_is_not_research_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.common, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "run_bounded", lambda *a: {"account": "PASS", "metadata": "PASS"})
    target = tmp_path / ".artifacts" / "success.json"
    assert probe.main(["--profile", "synthetic", "--url", URL, "--output", str(target)]) == 0
    report = json.loads(target.read_text())
    assert report["research_gate"] == "NEEDS_MORE_EVIDENCE"
    assert report["media_downloads"] == 0
    assert report["download_probe"] == "NOT_IMPLEMENTED"


def test_extractor_options_are_metadata_only_and_use_in_memory_cookies(monkeypatch):
    import yt_dlp
    captured = {}
    jar = http.cookiejar.CookieJar()
    class FakeYDL:
        def __init__(self, options, **kwargs):
            captured.update(options)
            assert kwargs == {"auto_init": False}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def add_info_extractor(self, ie):
            captured["ie"] = ie
        def extract_info(self, url, download):
            assert url == URL and download is False
            assert self.cookiejar is not jar
            assert list(self.cookiejar) == list(jar)
            assert callable(self.cookiejar.get_cookie_header)
            assert self.cookiejar.filename is None
            return entry()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(probe, "summarize", lambda info, observed, bvid: {"captured": True})
    assert probe.extract_metadata(URL, jar) == {"captured": True}
    assert captured["retries"] == captured["extractor_retries"] == 0
    assert captured["socket_timeout"] == 15
    assert captured["playlistend"] == 2
    assert captured["cachedir"] is False
    assert captured["skip_download"] is True
