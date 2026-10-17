import copy
import json
import os
from pathlib import Path

import pytest

from bilidesk.library import Library
from bilidesk import gallery_worker as worker
from bilidesk.gallery_engine import run_stage,Stop,digest
from bilidesk.gallery_transport import Guard
from bilidesk.gallery_store import load_draft,verify_cache,publish
from test_gallery_native_probe import native


@pytest.fixture
def app_case(native,tmp_path,monkeypatch):
    lib=Library(tmp_path/'library'); lib.initialize()
    tool=tmp_path/'ffmpeg.exe'; tool.write_bytes(b'fixture')
    tool.with_name('ffprobe.exe').write_bytes(b'fixture')
    root=tmp_path/'downloads'
    for k,v in {'offline':'0','gallery_online':'1','profile':'fixture','ffmpeg':str(tool),'root':str(root)}.items(): lib.set_setting(k,v)
    monkeypatch.setattr(worker,'load_bili_cookies',lambda *a:(native['jar'],{}))
    def engine(ident,phase,directory,jar,ffmpeg,**kw):
        guard=Guard('https://www.bilibili.com/opus/'+ident,kw.get('prior',{}).get('media_bytes',0) if kw.get('prior') else 0,
                    clock=native['time'].clock,sleep=native['time'].sleep)
        return run_stage(ident,phase,directory,jar,ffmpeg,guard=guard,verifier=lambda p,d:{'decode':'PASS'},**kw)
    monkeypatch.setattr(worker,'run_stage',engine)
    events=[]
    def execute(action,payload): return worker.execute(dict(data_dir=str(lib.directory),action=action,payload=payload),events.append)
    return lib,root,execute,events,native


def prepare(case):
    return case[2]('gallery_prepare',{'url':'https://www.bilibili.com/opus/1249456774436618276'})


def test_prepare_is_private_then_group_publication_and_duplicate_zero_requests(app_case):
    lib,root,execute,events,state=app_case
    prep=prepare(app_case)
    assert lib.snapshot()['total']==0
    result=execute('gallery_download',prep|{'selected':[1,3]})
    assert result['download']=='PASS'
    work=lib.detail(result['work_key'])
    assert work['saved_groups']==2 and work['group_count']==4 and work['selected_groups']==2 and work['selection_complete']
    assert lib.snapshot(undownloaded=True)['total']==0
    folder=root/'1337856191'/'图文／图片'/'1249456774436618276'
    assert {p.name for p in folder.iterdir()}=={'01','03'}
    count=len(state['calls'])
    assert execute('gallery_download',prep|{'selected':[1,3]})['download']=='ALREADY_PRESENT'
    assert len(state['calls'])==count


@pytest.mark.parametrize('change',['missing','changed','sha','empty','duplicate','out_of_range'])
def test_preflight_failures_send_no_network(app_case,change):
    lib,root,execute,events,state=app_case; prep=prepare(app_case)
    draft=load_draft(lib,prep['draft_id']); selected=[1]
    if change=='missing': Path(draft['assets'][0]['path']).unlink()
    elif change=='changed': Path(draft['assets'][0]['path']).write_bytes(b'changed')
    elif change=='sha': prep['manifest_sha']='0'*64
    elif change=='empty': selected=[]
    elif change=='duplicate': selected=[1,1]
    else: selected=[33]
    count=len(state['calls'])
    with pytest.raises(Stop): execute('gallery_download',prep|{'selected':selected})
    assert len(state['calls'])==count and lib.snapshot()['total']==0


@pytest.mark.parametrize('setting',['offline','online_stopped'])
def test_offline_and_stopped_workers_never_send(app_case,setting):
    app_case[0].set_setting(setting,'1')
    with pytest.raises(Stop): prepare(app_case)
    assert not app_case[4]['calls']


def test_existing_destination_conflict_preserves_both(app_case):
    lib,root,execute,events,state=app_case; prep=prepare(app_case)
    target=root/'1337856191'/'图文／图片'/'1249456774436618276'/'01'
    target.mkdir(parents=True); (target/'01.png').write_bytes(b'old')
    with pytest.raises(Stop,match='destination_conflict'): execute('gallery_download',prep|{'selected':[1]})
    assert (target/'01.png').read_bytes()==b'old'
    work=lib.detail('gallery:1249456774436618276')
    assert work['saved_groups']==0 and not work['selection_complete']


def test_v3_migration_selection_and_future_version(tmp_path):
    lib=Library(tmp_path/'lib'); lib.initialize()
    with lib.connection() as db: db.execute('PRAGMA user_version=2')
    lib.initialize();lib.initialize()
    with lib.connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==3
        db.execute('PRAGMA user_version=4')
    with pytest.raises(ValueError,match='library_version_newer'): lib.initialize()


def test_new_tasks_obey_offline_and_pause_allowlists(tmp_path):
    lib=Library(tmp_path/'lib');lib.initialize()
    a=lib.enqueue('gallery_download',{},'a'); b=lib.enqueue('gallery_prepare',{},'b')
    assert lib.next_task(local_only=True)['id']==b
    assert lib.next_task(offline_only=True) is None
    assert lib.next_task()['id']==a


def test_actual_ffmpeg_png_chain_and_local_decode(native,tmp_path):
    binary=os.environ.get('BILI_TEST_FFMPEG')
    if not binary: pytest.skip('Requires real ffmpeg/ffprobe pair')
    guard=Guard('https://www.bilibili.com/opus/1249456774436618276',clock=native['time'].clock,sleep=native['time'].sleep)
    # Real verifier uses real monotonic time; do not substitute its deadline.
    import time
    guard.deadline=time.monotonic()+180
    result=run_stage('1249456774436618276','prepare',native['directory'],native['jar'],binary,guard=guard)
    assert all(a['decode']['decoded_frames']>=1 for a in result['assets'])


def test_restart_never_dispatches_inherited_online_tasks(tmp_path):
    lib=Library(tmp_path/'lib');lib.initialize()
    for k in ('metadata','download','gallery_prepare','gallery_download'): lib.enqueue(k,{},k)
    lib.recover()
    assert lib.next_task() is None
    assert all(t['state']=='interrupted' for t in lib.tasks())


def test_uncertain_killed_attempt_is_not_retried(app_case):
    lib,root,execute,events,state=app_case;prep=prepare(app_case)
    draft=load_draft(lib,prep['draft_id']);draft['attempt_active']=True
    with lib.connection() as db: db.execute('UPDATE gallery_drafts SET payload=?',(json.dumps(draft),))
    count=len(state['calls'])
    with pytest.raises(Stop,match='gallery_previous_attempt_unconfirmed'): execute('gallery_download',prep|{'selected':[1]})
    assert len(state['calls'])==count
