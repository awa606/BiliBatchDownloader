import os
from pathlib import Path
import pytest
from scripts.smoke_gallery_product import run
from scripts.prepare_gallery_candidate import prepare
from bilidesk.library import Library
from bilidesk.media import digest


def test_actual_product_player_and_error(tmp_path):
    ffmpeg=os.environ.get('BILI_TEST_FFMPEG')
    if not ffmpeg: pytest.skip('BILI_TEST_FFMPEG required for actual product rendering')
    assert len(run(tmp_path/'product',ffmpeg))==2


def test_candidate_copy_preserves_original_and_interrupts_queue(tmp_path):
    old=Library(tmp_path/'old'); old.initialize(); old.enqueue('metadata',{},'inherited')
    before=digest(old.path)
    receipt=prepare(old.path,tmp_path/'candidate',[])
    assert receipt['source_unchanged'] and digest(old.path)==before
    candidate=Library(tmp_path/'candidate')
    assert candidate.setting('offline')=='1' and candidate.tasks()[0]['state']=='interrupted'
    assert old.tasks()[0]['state']=='queued'
    with pytest.raises(ValueError): prepare(old.path,tmp_path/'candidate',[])
