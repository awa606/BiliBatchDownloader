import json
from pathlib import Path
import pytest
from bilidesk.library import Library,identity
from bilidesk.media import digest,import_reports
from bilidesk.worker import execute

B='BV13x41117TL'

@pytest.fixture
def library(tmp_path):
    library=Library(tmp_path/'data'); library.initialize()
    library.save_work(dict(bvid=B,uid='10',title='标题',author='作者',tags=['标签'],tags_status='present',published=100),
                      [dict(cid='100',part=1,duration=12),dict(cid='101',part=2,duration=14)])
    return library

def test_stable_uid_rename_and_categories(library):
    library.classify(B,'投稿视频')
    library.save_work(dict(bvid=B,uid='10',title='新标题',author='改名'),[dict(cid='100',part=1)])
    work=library.detail(B)
    assert work['author']=='改名' and work['category']=='投稿视频'
    assert work['tags_status']=='present' and work['tags']==['标签']
    assert len(work['assets'])==2
    with pytest.raises(ValueError,match='conflict'):
        library.save_work(dict(bvid=B,uid='11'),[])

def test_snapshot_search_paging(library):
    assert library.snapshot(search='标签')['total']==1
    assert library.snapshot(search='%')['total']==0
    assert library.snapshot(category='动态视频')['total']==0
    assert library.snapshot(undownloaded=True)['total']==1
    assert library.snapshot(uid='11')['total']==0
    assert library.snapshot(offset=40)['works']==[]

def test_unknown_classification_and_group(library):
    assert library.detail(B)['category']=='待分类'
    library.group_author('10','课程')
    assert library.snapshot()['authors'][0]['group_name']=='课程'
    with pytest.raises(ValueError): library.classify(B,'mp4')

def test_duplicate_task_and_restart(library):
    first=library.enqueue('download',{'bvid':B},'asset:100')
    assert library.enqueue('download',{'bvid':B},'asset:100')==first
    library.update_task(first,state='running')
    library.recover()
    assert library.tasks()[0]['state']=='interrupted'
    assert library.enqueue('download',{'bvid':B},'asset:100')!=first

def test_pause_does_not_block_local_open(library):
    library.enqueue('download',{},'d')
    local=library.enqueue('check_file',{},'f')
    assert library.next_task(local_only=True)['id']==local
    assert library.next_task()['kind']=='download'

def test_missing_file_cannot_be_opened(library):
    with pytest.raises(ValueError,match='local_file_missing'):
        execute(dict(data_dir=str(library.directory),action='check_file',payload=dict(bvid=B,cid='100')))

def test_registered_file_and_missing_detection(library,tmp_path):
    path=tmp_path/'asset.mp4'; path.write_bytes(b'test')
    library.register_file(B,'100',dict(path=str(path),size=4,sha256=digest(path),duration=1))
    spec=dict(data_dir=str(library.directory),action='check_file',payload=dict(bvid=B,cid='100'))
    assert execute(spec)['path']==str(path)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'): execute(spec)
    path.unlink()
    with pytest.raises(ValueError,match='missing'): execute(spec)

def reports(tmp_path):
    root=tmp_path/'reports'; root.mkdir()
    video=root/'asset.mp4'; video.write_bytes(b'synthetic')
    picture=root/'frame.png'; picture.write_bytes(b'synthetic frame')
    source=root/'video.json'; preview=root/'preview.json'
    asset=dict(bvid=B,cid=100,part=1,author_uid='10',relative_file='asset.mp4',sha256=digest(video),size_bytes=9,
               duration_seconds=1,verification='PASS',full_decode='PASS')
    source.write_text(json.dumps(dict(download='PASS',assets=[asset])))
    preview.write_text(json.dumps(dict(preview='PASS',assets=[dict(source=asset,samples=[dict(index=1,relative_file='frame.png',
                       png_sha256=digest(picture),actual_pts_seconds=.5,duplicate_of=None)])])))
    return source,preview,video,picture

def test_report_import_in_place_and_idempotent(library,tmp_path):
    source,preview,video,picture=reports(tmp_path)
    before=(digest(video),digest(picture))
    for _ in range(2): assert import_reports(library,source,preview)['assets']==1
    assert (digest(video),digest(picture))==before
    work=library.detail(B)
    assert len(work['assets'])==2 and len(work['assets'][0]['previews'])==1
    assert work['title']=='标题'
    assert work['published']==100

@pytest.mark.parametrize('change',['hash','traversal','preview'])
def test_import_failure_no_registration(library,tmp_path,change):
    source,preview,video,picture=reports(tmp_path)
    if change=='hash': video.write_bytes(b'corrupt')
    elif change=='preview': picture.write_bytes(b'corrupt')
    else:
        data=json.loads(source.read_text()); data['assets'][0]['relative_file']='../outside.mp4'; source.write_text(json.dumps(data))
    with pytest.raises((ValueError,FileNotFoundError)): import_reports(library,source,preview)
    assert library.detail(B)['assets'][0]['status']=='remote'

@pytest.mark.parametrize('bvid,uid,cid',[('bad','10','1'),(B,'../','1'),(B,'10','-1')])
def test_identity_validation(bvid,uid,cid):
    with pytest.raises(ValueError): identity(bvid,uid,cid)
