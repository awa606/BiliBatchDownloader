"""One registered native download, fixed sample; parent bounds whole process tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bilidesk.library import Library
from bilidesk.process import Job


def fingerprint(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--profile',required=True)
    parser.add_argument('--ffmpeg',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    output=args.output.resolve()
    if not output.is_relative_to((ROOT/'.artifacts').resolve()) or output.exists(): raise ValueError('new_artifacts_directory_required')
    output.mkdir()
    sources=['experiments/native_video_engine.py','experiments/native_video_probe.py','src/bilidesk/worker.py',
             'src/bilidesk/process.py','src/bilidesk/media.py','requirements-research.lock.txt']
    registry={'experiment':'NATIVE_VIDEO_01','registered_utc':datetime.now(timezone.utc).isoformat(),
      'bvid':'BV13x41117TL','part':1,'cid':'14694589','max_height':360,'max_requests':12,'max_seconds':230,
      'max_network_wait':15,'max_wire_bytes':64*1024*1024,'retries':0,
      'stop':'login failure, 401/403/412/429, parsed verification page, identity mismatch or any budget exhausted',
      'source_sha256':{p:fingerprint(ROOT/p) for p in sources},'ffmpeg_sha256':fingerprint(args.ffmpeg),
      'baseline_sha256':fingerprint(ROOT/'.artifacts/video-download-u02-gzip-20261016-attempt1/report.json')}
    (output/'registration.json').write_text(json.dumps(registry,indent=2),encoding='utf-8')
    lib=Library(output/'worker-data'); lib.initialize()
    spec={'data_dir':str(lib.directory),'action':'native_probe','payload':dict(bvid='BV13x41117TL',part=1,cid='14694589',
           profile=args.profile,ffmpeg=str(args.ffmpeg.resolve()),root=str(output/'media'))}
    env=dict(os.environ,PYTHONPATH=str(ROOT/'src'))
    report={'experiment':'NATIVE_VIDEO_01','status':'RUNNING','requests':None,'media_scope':'one fixed ordinary P1',
            'registration_sha256':fingerprint(output/'registration.json')}
    (output/'report.json').write_text(json.dumps(report),encoding='utf-8')
    job=Job(); process=None
    try:
        process=subprocess.Popen([sys.executable,'-m','bilidesk.worker'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,env=env,cwd=ROOT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job.assign(process.pid)
        try:
            stdout,stderr=process.communicate((json.dumps(spec)+'\n').encode('utf-8'),timeout=230)
        except subprocess.TimeoutExpired:
            job.close(); process.kill(); stdout,stderr=process.communicate(timeout=5)
            report.update(status='FAIL',reason='worker_timeout')
        for line in stdout.splitlines():
            try: event=json.loads(line)
            except Exception: report.update(status='FAIL',reason='worker_protocol_error'); continue
            if event.get('type')=='budget': report['budget']={k:v for k,v in event.items() if k!='type'}
            elif event.get('type')=='result': report.update(status='PASS',result=event['result'])
            elif event.get('type')=='error': report.update(status='FAIL',reason=event['reason'])
        if report['status']=='RUNNING': report.update(status='FAIL',reason='worker_exited_without_result')
        if stderr: report['discarded_stderr_bytes']=len(stderr)
    finally:
        job.close()
        report['finished_utc']=datetime.now(timezone.utc).isoformat()
        report['source_unchanged']=all(fingerprint(ROOT/p)==sha for p,sha in registry['source_sha256'].items())
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report.get(k) for k in ('status','reason','budget','source_unchanged')},ensure_ascii=False))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__': raise SystemExit(main())
