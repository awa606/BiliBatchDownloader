"""Register/execute exactly one fixed four-stage product check; stop at first failure."""
import argparse
from datetime import datetime,timezone
from importlib import import_module,metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'experiments')]
from bilidesk.library import Library
from bilidesk.gallery_engine import digest
from bilidesk.gallery_store import load_draft,checked
from bilidesk.process import Job
from gallery_native_probe import load_baseline,SAMPLES

SAMPLE_ROWS=[('images','1249456774436618276',[1,3],6,2,'8afb9ed55ff35e3ed17eb94709704709532cbab5ed321b58c6ff44ba9d70550f'),
 ('livephoto','1257912749013336118',[1,5,9],11,5,'a98aaf48db64d4c85bc5baeaf7ccc59a0c609d699d60b34cdcc345ded8dd3b5b')]


def worker_environment(original):
    env=dict(original)
    windows=Path(env.get('SystemRoot') or env.get('SYSTEMROOT') or 'C:/Windows')
    env['PATH']=os.pathsep.join([str(windows/'System32'),str(windows)])
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QML2_IMPORT_PATH'):env.pop(key,None)
    return env


def fingerprints(bundle,ffmpeg):
    files=[p for folder in ('src','scripts','resources') for p in (ROOT/folder).rglob('*')
           if p.suffix in ('.py','.qml') and '__pycache__' not in p.parts]+[Path(__file__)]
    result={str(p.relative_to(ROOT)):digest(p) for p in files}
    for name,_,_,_,_,expected in SAMPLE_ROWS:
        p=ROOT/f'.artifacts/U03-gallery-user-{name}-20261016-attempt1.json'
        assert digest(p)==expected
        result[str(p.relative_to(ROOT))]=expected
    for name in ('gallery_dl.job','gallery_dl.extractor.bilibili','gallery_dl.downloader.http','requests.adapters','urllib3.response'):
        result[name]=digest(Path(import_module(name).__file__))
    result['worker']=digest(bundle/'gallery-worker/BiliGalleryWorker.exe')
    result['ffmpeg']=digest(ffmpeg);result['ffprobe']=digest(ffmpeg.with_name('ffprobe.exe'))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--bundle',required=True,type=Path)
    p.add_argument('--source-library',required=True,type=Path);p.add_argument('--execute',action='store_true');a=p.parse_args()
    out=a.output.resolve();bundle=a.bundle.resolve();source=Library(a.source_library.resolve())
    ffmpeg=Path(source.setting('ffmpeg'));assert out.is_relative_to(ROOT/'.artifacts')
    prints=fingerprints(bundle,ffmpeg)
    if not a.execute:
        out.mkdir(parents=True,exist_ok=False)
        registration=dict(registered_utc=datetime.now(timezone.utc).isoformat(),fingerprints=prints,
            source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            dependencies={k:metadata.version(k) for k in ('gallery-dl','requests','urllib3','yt-dlp')},
            stages=[dict(sample=r[1],selected=r[2],prepare_limit=r[3],download_limit=r[4]) for r in SAMPLE_ROWS],
            total_request_limit=24,retries=0,redirects=0,worker_seconds=180,network_wait=15,interval_seconds=1,
            page_bytes=3*1024**2,asset_bytes=64*1024**2,sample_media_bytes=256*1024**2,
            stop='first failure stops all remaining stages; no replacement or retry')
        (out/'registration.json').write_text(json.dumps(registration,indent=2),encoding='utf-8')
        print(json.dumps({'status':'REGISTERED','requests':0}));return
    registration=json.loads((out/'registration.json').read_text(encoding='utf-8'))
    assert prints==registration['fingerprints']
    with (out/'execution-started.json').open('x',encoding='utf-8') as f:json.dump({'utc':datetime.now(timezone.utc).isoformat()},f)
    lib=Library(out/'private-library');lib.initialize()
    for key in ('profile','ffmpeg'):lib.set_setting(key,source.setting(key))
    for key,value in dict(root=str(out/'media'),offline='0',gallery_online='1',paused='1').items():lib.set_setting(key,value)
    env=worker_environment(os.environ)
    report={'status':'IN_PROGRESS','registration_sha256':digest(out/'registration.json'),'stages':[],'total_requests':0}
    try:
        for name,ident,selected,prepare_limit,download_limit,_ in SAMPLE_ROWS:
            baseline=load_baseline(ROOT/f'.artifacts/U03-gallery-user-{name}-20261016-attempt1.json',SAMPLES[name])
            prior=None
            for phase,limit in [('prepare',prepare_limit),('download',download_limit)]:
                payload={'url':'https://www.bilibili.com/opus/'+ident} if phase=='prepare' else prior|{'selected':selected}
                spec=dict(data_dir=str(lib.directory),action='gallery_'+phase,payload=payload,baseline=baseline)
                row=dict(sample=ident,phase=phase,status='FAIL',request_limit=limit);report['stages'].append(row)
                time.sleep(1)  # Keep the request-start gap across separate workers too.
                process=subprocess.Popen([str(bundle/'gallery-worker/BiliGalleryWorker.exe')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                job=Job();job.assign(process.pid)
                try:
                    stdout,_=process.communicate((json.dumps(spec)+'\n').encode(),timeout=180)
                except subprocess.TimeoutExpired:
                    job.close();process.kill();process.wait();row.update(reason='worker_timeout',requests=None);report['total_requests']=None;return
                finally:job.close()
                events=[json.loads(line) for line in stdout.splitlines()]
                (out/f'{name}-{phase}-events.json').write_text(json.dumps(events,indent=2),encoding='utf-8')
                budget=next((e for e in events if e.get('type')=='budget'),None)
                count=budget['network_requests'] if budget else None
                row.update(requests=count,budget=budget)
                row['metadata_diagnostics']=[e for e in events if e.get('type')=='metadata_diagnostic']
                report['total_requests']=None if count is None else report['total_requests']+count
                error=next((e for e in events if e.get('type')=='error'),None)
                result=next((e['result'] for e in events if e.get('type')=='result'),None)
                if error or process.returncode or not result or count!=limit or report['total_requests']>24:
                    row['reason']=error['reason'] if error else 'worker_or_budget_contract_failed';return
                if phase=='prepare':
                    prior=result;draft=load_draft(lib,result['draft_id']);m=draft['metadata']
                    row.update(description_status=m['description_status'],description_chars=len(m['description']),
                        description_sha256=__import__('hashlib').sha256(m['description'].encode()).hexdigest(),
                        tags_status=m['tags_status'],tags_count=len(m['tags']),topics_count=len(m['topics']),manifest_sha=draft['manifest_sha'])
                else:
                    work=lib.detail('gallery:'+ident)
                    assert work['saved_groups']==len(selected) and work['selection_complete']
                    for group in work['groups']:
                        assert bool(group['image'])==(group['sequence'] in selected)
                        for role in ('image','live_video'):
                            if group[role]:checked(group[role])
                    row.update(saved_groups=work['saved_groups'],total_groups=work['group_count'],selection_complete=True)
                row['status']='PASS';print(json.dumps({k:row[k] for k in ('sample','phase','status','requests')}),flush=True)
        report['status']='PASS'
    except BaseException as exc:
        report['reason']='verification_'+type(exc).__name__
    finally:
        if report['status']=='IN_PROGRESS':report['status']='STOPPED'
        report['source_fingerprints_unchanged']=prints==fingerprints(bundle,ffmpeg)
        (out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({'status':report['status'],'requests':report['total_requests']}),flush=True)


if __name__=='__main__':main()
