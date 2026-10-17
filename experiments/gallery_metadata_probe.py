"""Preregister one frozen-worker metadata inspection: two requests, no media."""
import argparse
from datetime import datetime, timezone
from importlib import import_module, metadata
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'experiments')]
from bilidesk.gallery_engine import digest
from bilidesk.gallery_store import safe_path
from bilidesk.library import Library
from bilidesk.process import Job
from gallery_native_probe import load_baseline, SAMPLES
from gallery_product_verify import worker_environment

BASELINE = ROOT/'.artifacts/U03-gallery-user-images-20261016-attempt1.json'
BASELINE_SHA = '8afb9ed55ff35e3ed17eb94709704709532cbab5ed321b58c6ff44ba9d70550f'
PRIOR = ROOT/'.artifacts/gallery-product-online-20261017-attempt2/report.json'
PRIOR_SHA = 'b7b898ccb3a6b4e3a04b14b8aa40c71263e920c4fa1f5377629b31dc5579426d'


def fingerprints(worker, library):
    if digest(BASELINE) != BASELINE_SHA or digest(PRIOR) != PRIOR_SHA:
        raise ValueError('historical_fingerprint_changed')
    paths = [p for folder in ('src','experiments','scripts','tests') for p in (ROOT/folder).rglob('*.py')]
    result = {str(p.relative_to(ROOT)):digest(p) for p in paths}
    for name in ('gallery_dl.extractor.bilibili','gallery_dl.extractor.common','gallery_dl.job',
                 'requests.adapters','urllib3.response'):
        result[name] = digest(Path(import_module(name).__file__))
    result['bundle'] = {str(p.relative_to(worker.parent)):digest(p) for p in worker.parent.rglob('*') if p.is_file()}
    result.update(baseline=BASELINE_SHA,prior_failure=PRIOR_SHA,source_library=digest(library.path))
    return result


def run_worker(worker, spec, *, timeout=60):
    process = None
    job = Job()
    try:
        process = subprocess.Popen([str(worker),'--inspect-metadata'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env=worker_environment(os.environ),
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job.assign(process.pid)
        stdout,_ = process.communicate((json.dumps(spec)+'\n').encode(),timeout=timeout)
        if len(stdout)>1024*1024: return dict(diagnostic='NOT_COMPLETED',stop_reason='worker_output_limit',budget=None)
        events = [json.loads(line) for line in stdout.splitlines()]
        if process.returncode or len(events)!=1 or events[0].get('type')!='result':
            return dict(diagnostic='NOT_COMPLETED',stop_reason='worker_failed',budget=None)
        return events[0]['result']
    except subprocess.TimeoutExpired:
        return dict(diagnostic='NOT_COMPLETED',stop_reason='worker_timeout',budget=None)
    except BaseException:
        return dict(diagnostic='NOT_COMPLETED',stop_reason='worker_launch_or_protocol_failed',budget=None)
    finally:
        job.close()
        if process and process.poll() is None:
            process.kill();process.wait()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--worker',required=True,type=Path)
    parser.add_argument('--source-library',required=True,type=Path)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    output=safe_path(args.output);worker=safe_path(args.worker)
    if not output.is_relative_to(ROOT/'.artifacts'): raise ValueError('output_outside_artifacts')
    library=Library(safe_path(args.source_library))
    if not library.path.is_file() or not worker.is_file(): raise ValueError('input_missing')
    current=fingerprints(worker,library)
    baseline=load_baseline(BASELINE,SAMPLES['images'])
    if not args.execute:
        output.mkdir(parents=True,exist_ok=False)
        record=dict(schema=1,registered_utc=datetime.now(timezone.utc).isoformat(),fingerprints=current,
            source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            dependencies={k:metadata.version(k) for k in ('gallery-dl','requests','urllib3')},
            sample='1249456774436618276',author_uid='1337856191',requests=2,media_requests=0,media_bytes=0,
            retries=0,redirects=0,worker_seconds=60,network_wait=15,interval_seconds=1,page_bytes=3*1024**2,
            stop='one execution only; no media; no network after failure; in-memory comparison may finish')
        (output/'registration.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
        print(json.dumps(dict(status='REGISTERED',requests=0)));return
    registration=json.loads((output/'registration.json').read_text(encoding='utf-8'))
    if current!=registration['fingerprints']: raise ValueError('registered_fingerprint_changed')
    with (output/'execution-started.json').open('x',encoding='utf-8') as stream:
        json.dump({'utc':datetime.now(timezone.utc).isoformat()},stream)
    result=run_worker(worker,dict(profile=library.setting('profile'),directory=str(output),baseline=baseline))
    result['registration_sha256']=digest(output/'registration.json')
    result['fingerprints_unchanged']=current==fingerprints(worker,library)
    result['request_count_known']=bool(result.get('budget'))
    prior=json.loads(PRIOR.read_text(encoding='utf-8'))
    prior_page=next(r for r in prior['stages'][0]['budget']['requests'] if r['kind']=='page')
    pages=[r for r in (result.get('budget') or {}).get('requests',[]) if r['kind']=='page' and r.get('complete')]
    result['prior_raw_equal']=pages[0]['raw_sha256']==prior_page['raw_sha256'] if pages else None
    result['prior_decoded_equal']=pages[0]['decoded_sha256']==prior_page['decoded_sha256'] if pages else None
    result['download_acceptance']='NOT_RUN'
    (output/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({key:result.get(key) for key in ('diagnostic','stop_reason','request_count_known','prior_raw_equal','prior_decoded_equal')}))


if __name__=='__main__': main()
