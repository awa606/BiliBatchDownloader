import os
import pytest
from scripts.smoke_gallery_online import run


def test_actual_selection_and_native_pipeline(tmp_path):
    ffmpeg=os.environ.get('BILI_TEST_FFMPEG')
    if not ffmpeg:pytest.skip('BILI_TEST_FFMPEG required')
    assert run(tmp_path/'selection',ffmpeg)['real_network_requests']==0
