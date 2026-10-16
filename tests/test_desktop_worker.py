import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
from bilidesk.library import Library
from bilidesk.media import digest,inspect,previews
from bilidesk.worker import execute
from bilidesk.process import Job
import bilidesk.native as native

B='BV13x41117TL'

@pytest.fixture
def synthetic(tmp_path):
    tool=os.environ.get('BILI_TEST_FFMPEG')
    if not tool: pytest.skip('explicit FFmpeg required')
    target=tmp_path/'source.mp4'
    subprocess.run([tool,'-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=10',
       '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','2','-c:v','libx264','-c:a','aac',str(target)],
       capture_output=True,check=True,timeout=20)
    return target,tool

def test_synthetic_decode_and_actual_pts(synthetic,tmp_path):
    source,ffmpeg=synthetic
    info=inspect(source,ffmpeg,2)
    frames=previews(source,info['duration'],tmp_path/'preview',ffmpeg)
    assert len(frames)==1 and frames[0]['seconds']>=frames[0]['requested_seconds']-.001
    assert info['video_tracks']==info['audio_tracks']==1

def test_worker_publish_then_duplicate_without_network(synthetic,tmp_path,monkeypatch):
    source,ffmpeg=synthetic
    lib=Library(tmp_path/'data'); lib.initialize()
    calls=[]
    def fake_native(**kwargs):
        calls.append(1); shutil.copy2(source,Path(kwargs['stage'])/'asset.mp4')
        return {'duration':2,'work':dict(bvid=B,uid='10',cid='100',title='合成作品',parts=[dict(cid='100',part=1,duration=2)])}
    monkeypatch.setattr(native,'run_native',fake_native)
    payload=dict(bvid=B,part=1,cid='100',root=str(tmp_path/'downloads'),profile='synthetic',ffmpeg=ffmpeg)
    spec=dict(data_dir=str(lib.directory),action='download',payload=payload)
    result=execute(spec,emit=lambda e:None)
    assert result['download']=='PASS' and result['preview']=='PASS'
    asset=lib.detail(B)['assets'][0]
    assert Path(asset['path']).relative_to(tmp_path/'downloads').parts==('10','待分类',B,'100','asset.mp4')
    assert digest(source)==digest(asset['path'])
    assert execute(spec)['download']=='ALREADY_PRESENT' and len(calls)==1

def test_preview_failure_preserves_download(synthetic,tmp_path,monkeypatch):
    source,ffmpeg=synthetic; lib=Library(tmp_path/'data'); lib.initialize()
    def fake_native(**kwargs):
        shutil.copy2(source,Path(kwargs['stage'])/'asset.mp4')
        return {'duration':2,'work':dict(bvid=B,uid='10',cid='100',parts=[dict(cid='100',part=1,duration=2)])}
    monkeypatch.setattr(native,'run_native',fake_native)
    import bilidesk.worker as worker
    def failed(*args): raise ValueError('synthetic_preview_failure')
    monkeypatch.setattr(worker,'previews',failed)
    result=execute(dict(data_dir=str(lib.directory),action='download',payload=dict(bvid=B,part=1,cid='100',root=str(tmp_path/'out'),profile='fake',ffmpeg=ffmpeg)),emit=lambda e:None)
    assert result['preview']=='FAILED_VIDEO_RETAINED'
    assert lib.detail(B)['assets'][0]['status']=='complete'

def test_existing_different_destination_not_overwritten(synthetic,tmp_path,monkeypatch):
    source,ffmpeg=synthetic; lib=Library(tmp_path/'data'); lib.initialize()
    def fake_native(**kwargs):
        shutil.copy2(source,Path(kwargs['stage'])/'asset.mp4')
        return {'duration':2,'work':dict(bvid=B,uid='10',cid='100',parts=[dict(cid='100',part=1,duration=2)])}
    monkeypatch.setattr(native,'run_native',fake_native)
    target=tmp_path/'out'/'10'/'待分类'/B/'100'/'asset.mp4'; target.parent.mkdir(parents=True); target.write_bytes(b'original')
    with pytest.raises(ValueError,match='destination_conflict'):
        execute(dict(data_dir=str(lib.directory),action='download',payload=dict(bvid=B,part=1,cid='100',root=str(tmp_path/'out'),profile='fake',ffmpeg=ffmpeg)),emit=lambda e:None)
    assert target.read_bytes()==b'original'
    assert lib.detail(B)['assets'][0]['status']=='remote'

@pytest.mark.skipif(os.name!='nt',reason='Windows Job required')
def test_private_job_cancels_real_worker(tmp_path):
    lib=Library(tmp_path/'data'); lib.initialize()
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    job=Job()
    child=subprocess.Popen([sys.executable,'-m','bilidesk.worker'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
         env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        job.assign(child.pid)
        child.stdin.write((json.dumps(dict(data_dir=str(lib.directory),action='synthetic_wait'))+'\n').encode()); child.stdin.flush()
        time.sleep(.25)
        assert child.poll() is None
        started=time.monotonic()
        job.close(); child.wait(timeout=5)
        assert time.monotonic()-started<5
        assert b'"type": "result"' not in child.stdout.read()
    finally:
        job.close()
        if child.poll() is None: child.kill(); child.wait()
        child.stdin.close(); child.stdout.close(); child.stderr.close()

def test_worker_redacts_exception(tmp_path):
    lib=Library(tmp_path/'data'); lib.initialize()
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    result=subprocess.run([sys.executable,'-m','bilidesk.worker'],input=json.dumps(dict(data_dir=str(lib.directory),action='import',
       payload=dict(source='SENTINEL_SECRET_NO_FILE',preview='SENTINEL_COOKIE')))+'\n',capture_output=True,text=True,env=env,timeout=10)
    assert result.returncode==1 and 'SENTINEL' not in result.stdout+result.stderr
    assert json.loads(result.stdout)['type']=='error'
