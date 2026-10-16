import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "experiments"))
import gallery_probe as probe


@pytest.mark.parametrize("url", ["https://evil.test/opus/123", "https://www.bilibili.com/opus/123?token=secret", "file:///test"])
def test_url_boundary(url):
    with pytest.raises(ValueError):
        probe.normalized_url(url)


def test_dynamic_alias_normalizes():
    assert probe.normalized_url("https://t.bilibili.com/123") == "https://www.bilibili.com/opus/123"


def test_asset_summary_never_contains_signed_url():
    summary = probe.summarize_message((3, "https://i0.hdslb.com/bfs/test/image.gif?token=SYNTHETIC_PRIVATE", {"num": 1}))
    assert summary["format"] == "gif"
    assert summary["sequence"] == 1
    assert "SYNTHETIC_PRIVATE" not in str(summary)
    assert "https" not in str(summary)


def test_nonasset_is_not_media():
    assert probe.summarize_message((2, "", {"count": 2})) is None


@pytest.mark.parametrize("value", [None, "None", True, 0, -1, "abc"])
def test_missing_author_cannot_pass(value):
    with pytest.raises(probe.ProbeStop, match="missing_author_identity"):
        probe.author_identity(value)


def test_http_error_keeps_status_and_hash_without_sensitive_data():
    from types import SimpleNamespace
    error = RuntimeError("SYNTHETIC_PRIVATE_URL")
    error.response = SimpleNamespace(status_code=412, content=b"SYNTHETIC_PRIVATE_BODY")
    failure = probe.safe_http_failure(error)
    assert failure.evidence["http_status"] == 412
    assert len(failure.evidence["response_sha256"]) == 64
    assert "SYNTHETIC_PRIVATE" not in str(failure.evidence)
