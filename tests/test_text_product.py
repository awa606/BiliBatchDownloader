from scripts.smoke_text import run


def test_actual_video_gallery_and_selection_text(tmp_path):
    result=run(tmp_path/'text')
    assert result['network_requests']==0
    assert len(result['cases'])==3
