"""One explicitly authorized metadata-only refresh of the existing fixed work.

The standard yt-dlp extraction runs once. No alternate client, download, FFmpeg
or HTTP rewriting; unknown hosts are stopped before physical transmission.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bilidesk.library import Library
from bilidesk.media import digest
from bilidesk.native import Budget,Stop,run_native
from bilidesk.process import Job

BVID='BV1bK411W797'
UID='150259984'
PARTS={(1,'191557669'),(2,'191559297')}


def validate(work):
    if work.get('bvid')!=BVID or work.get('uid')!=UID: raise Stop('work_identity_mismatch')
    parts=work.get('parts',[])
    if not isinstance(parts,list): raise Stop('parts_identity_mismatch')
    if not PARTS.issubset({(p.get('part'),p.get('cid')) for p in parts}): raise Stop('parts_identity_mismatch')
    if len({p['part'] for p in parts})!=len(parts) or len({p['cid'] for p in parts})!=len(parts):
        raise Stop('parts_identity_mismatch')
    if not isinstance(work.get('description'),str) or not work['description'].strip(): raise Stop('description_missing')
    if type(work.get('published')) is not int or work['published']<=0: raise Stop('publication_missing')
    if work.get('tags_status')!='present' or not isinstance(work.get('tags'),list) or not work['tags']:
        raise Stop('tags_missing')
    if any(not isinstance(t,str) or not t for t in work['tags']): raise Stop('tags_invalid')


def summary(work):
    validate(work)
    return dict(bvid=BVID,uid=UID,existing_cids_match=True,source_parts=len(work['parts']),
        description_chars=len(work['description']),description_sha256=hashlib.sha256(work['description'].encode()).hexdigest(),
        publication_present=True,tags_count=len(work['tags']),
        tags_sha256=hashlib.sha256(json.dumps(work['tags'],ensure_ascii=False).encode()).hexdigest())


def child(directory,library):
    output=Path(directory);lib=Library(library);budget=Budget(metadata_only=True)
    report=dict(status='FAIL',reason='metadata_not_completed',media_requests=0)
    try:
        result=run_native(bvid=BVID,part=1,expected_cid='191557669',profile=lib.setting('profile'),
            ffmpeg=lib.setting('ffmpeg'),stage=output/'unused-stage',download=False,budget=budget)
        report.update(status='PASS',reason=None,fields=summary(result['work']))
        # Private evidence, never stdout nor committed. Publication requires identity checks again.
        (output/'private-work.json').write_text(json.dumps(result['work'],ensure_ascii=False),encoding='utf-8')
    except BaseException as exc:
        report.update(status='FAIL',reason=exc.reason if isinstance(exc,Stop) else 'metadata_operation_failed')
    finally:
        report['budget']=budget.summary()
        (output/'child-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return 0 if report['status']=='PASS' else 1


def run(output,library):
    output=Path(output).resolve();lib=Library(library)
    if output.exists() or not output.is_relative_to((ROOT/'.artifacts').resolve()): raise ValueError('new_evidence_directory_required')
    current=lib.detail(BVID)
    if current.get('uid')!=UID or {(a['part'],a['cid']) for a in current['assets']}!=PARTS:
        raise ValueError('fixed_candidate_identity_required')
    output.mkdir()
    sources=['src/bilidesk/native.py','src/bilidesk/library.py','experiments/video_metadata_refresh.py','requirements-desktop.lock.txt',
             '.venv/Lib/site-packages/yt_dlp/extractor/bilibili.py']
    baseline=ROOT/'.artifacts/U02-video-multipart-20261016-attempt1.json'
    registry=dict(registered_utc=datetime.now(timezone.utc).isoformat(),bvid=BVID,uid=UID,
        parts=sorted(PARTS),max_requests=12,max_seconds=230,network_timeout=15,wire_bytes=64*1024*1024,
        retries=0,media_requests=0,redirects='native; each hop counts toward 12',
        stop='any error; 401/403/412/429, login, risk, identity, field or budget failure; no retry',
        sources={name:digest(ROOT/name) for name in sources},baseline_sha256=digest(baseline))
    (output/'registration.json').write_text(json.dumps(registry,indent=2),encoding='utf-8')
    report=dict(status='FAIL',reason='worker_not_completed',requests=None,media_requests=0,
                registration_sha256=digest(output/'registration.json'))
    process=None;job=Job()
    try:
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--child','--output',str(output),'--library',str(lib.directory)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job.assign(process.pid)
        try:
            stdout,stderr=process.communicate(timeout=230)
            path=output/'child-report.json'
            if path.is_file(): report.update(json.loads(path.read_text()))
            if process.returncode and report['status']=='PASS': report.update(status='FAIL',reason='worker_exit_failed')
            report.update(discarded_stdout_bytes=len(stdout),discarded_stderr_bytes=len(stderr))
            report['requests']=report.get('budget',{}).get('requests')
        except subprocess.TimeoutExpired:
            job.close();process.kill();process.communicate(timeout=5)
            report.update(status='FAIL',reason='worker_timeout',requests=None)
    finally:
        job.close()
        report['sources_unchanged']=all(digest(ROOT/n)==h for n,h in registry['sources'].items())
        if not report['sources_unchanged']:report.update(status='FAIL',reason='source_changed')
        report['finished_utc']=datetime.now(timezone.utc).isoformat()
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--library',required=True,type=Path)
    p.add_argument('--child',action='store_true');a=p.parse_args()
    if a.child:raise SystemExit(child(a.output,a.library))
    r=run(a.output,a.library);print(json.dumps(r));raise SystemExit(0 if r['status']=='PASS' else 1)
