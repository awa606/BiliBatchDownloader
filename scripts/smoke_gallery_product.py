"""Synthetic actual product playback, for source and frozen Windows runs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtGui import QImage,QColor
from bilidesk.library import Library
from bilidesk.gallery import import_report
from bilidesk.media import digest


def fixture(root,ffmpeg,broken=False):
    media=root/'media'; media.mkdir(parents=True)
    assets=[]
    for n in range(1,3):
        photo=media/f'{n:02d}.jpg'; video=media/f'{n:02d}l.mp4'
        image=QImage(320,240,QImage.Format_RGB32)
        for y in range(240):
            for x in range(320): image.setPixelColor(x,y,QColor(x%255,y%255,80+n*40))
        assert image.save(str(photo))
        if broken: video.write_bytes(b'PRIVATE_SYNTHETIC_SENTINEL broken mp4')
        else:
            subprocess.run([str(ffmpeg),'-nostdin','-v','error','-f','lavfi','-i','testsrc=size=160x120:rate=20',
                '-t','1.2','-c:v','libx264','-pix_fmt','yuv420p',str(video)],check=True,capture_output=True,timeout=20)
        for p,role in ((photo,'image'),(video,'live_video')):
            assets.append(dict(sequence=n,suffix='l' if role=='live_video' else '',format=p.suffix[1:],filename=p.name,
                pair_role=role,sha256=digest(p),source_path_sha256=f'{n*2+(role=="live_video"):064x}',size=p.stat().st_size,
                download='PASS',decode='PASS',decoded_frames=24 if role=='live_video' else 1,duration_seconds=1.2 if role=='live_video' else None))
    report=dict(schema_version=2,sample_kind='livephoto',status='PASS',baseline_match='PASS',account='PASS',fingerprints_unchanged=True,
        sample_dynamic_id='1001',author_uid='1002',declared_images=2,assets=assets,
        pairs=[dict(sequence=n,status='PASS',image_filename=f'{n:02d}.jpg',video_filename=f'{n:02d}l.mp4') for n in range(1,3)])
    path=root/'report.json'; path.write_text(json.dumps(report),encoding='utf-8')
    lib=Library(root/'data'); lib.initialize(); lib.set_setting('offline','1'); import_report(lib,path,digest(path))
    return lib


def run(output,ffmpeg,bundle=None):
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy(); windows=Path(env.get('SystemRoot','C:/Windows'))
    env['QT_QPA_PLATFORM']='windows'
    for key in ('QT_QUICK_BACKEND','QML2_IMPORT_PATH','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','PYTHONHOME'):
        env.pop(key,None)
    root=Path(__file__).resolve().parents[1]
    if bundle:
        command=[str(Path(bundle).resolve()/'BiliBatchDownloader.exe')]
        env.pop('PYTHONPATH',None); env['PATH']=os.pathsep.join([str(windows/'System32'),str(windows)])
    else:
        command=[sys.executable,'-m','bilidesk.app']; env['PYTHONPATH']=str(root/'src')
    results=[]
    for broken in (False,True):
        case=output/('corrupt' if broken else 'valid'); lib=fixture(case,ffmpeg,broken)
        process=subprocess.run([*command,'--data-dir',str(lib.directory),'--offline','--require-existing-library',
             '--gallery-smoke-output',str(case/'ui'),*(['--gallery-smoke-expect-error'] if broken else [])],
             capture_output=True,env=env,timeout=50,cwd=output)
        assert process.returncode==0
        assert b'PRIVATE_SYNTHETIC_SENTINEL' not in process.stdout+process.stderr
        assert str(case).encode() not in process.stdout+process.stderr
        report=json.loads((case/'ui/report.json').read_text(encoding='utf-8'))
        assert report['status']=='PASS',report
        assert report['frozen']==bool(bundle) and report['offline']
        if not broken:
            assert len(report['assets'])==4 and report['switch_during_playback'] and report['pause_resume']
        else: assert report['assets'][-1]['error']=='EXPECTED_PLAYER_ERROR'
        results.append(dict(broken=broken,status='PASS',frozen=bool(bundle)))
    (output/'report.json').write_text(json.dumps(dict(status='PASS',cases=results,network_requests=0),indent=2),encoding='utf-8')
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',required=True,type=Path); p.add_argument('--ffmpeg',required=True,type=Path)
    p.add_argument('--bundle',type=Path); a=p.parse_args(); print(json.dumps(run(a.output,a.ffmpeg,a.bundle)))
