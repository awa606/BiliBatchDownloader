import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "experiments"))
import dynamic_probe as probe


def item(uid, identifier):
    return {"id_str": identifier, "modules": {"module_author": {"mid": uid}, "module_dynamic": {}}}


def test_repost_preserves_both_authors():
    outer = item(1, "10")
    outer["orig"] = item(2, "11")
    result = probe.summarize(outer)
    assert result["publisher_uid"] == "1"
    assert result["original"]["publisher_uid"] == "2"


def test_images_preserve_order_without_signed_urls():
    data = item(1, "10")
    data["modules"]["module_dynamic"]["major"] = {"type": "MAJOR_TYPE_DRAW", "draw": {"items": [
        {"src": "https://i0.hdslb.com/test/a.gif?secret=SYNTHETIC_PRIVATE"},
        {"src": "https://i0.hdslb.com/test/b.png"}]}}
    result = probe.summarize(data)
    assert [i["format"] for i in result["images"]] == ["gif", "png"]
    assert "SYNTHETIC_PRIVATE" not in str(result)


def test_missing_author_stops():
    with pytest.raises(probe.ProbeStop):
        probe.summarize(item(None, "10"))


def test_dynamic_client_does_not_allow_followings_or_arbitrary_endpoint():
    assert "/x/relation/followings" not in probe.DynamicClient.allowed_paths
    assert len(probe.DynamicClient.allowed_paths) == 2
