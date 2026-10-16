"""One task per private process; stdin spec, JSONL events, no upstream logging."""
import json
import os
from pathlib import Path
import sys
import time
import uuid

from .library import Library,identity
from .media import import_reports,inspect,previews,digest,check_local_file


def emit(value):
    sys.stdout.write(json.dumps(value,ensure_ascii=False)+'\n'); sys.stdout.flush()


def execute(spec,emit=emit):
    library=Library(spec['data_dir']); action=spec['action']; payload=spec.get('payload',{})
    if action=='import':
        return import_reports(library,payload['source'],payload['preview'])
    if action=='check_file':
        return check_local_file(library,payload['bvid'],payload['cid'],payload.get('folder',False))
    if action=='synthetic_wait':
        time.sleep(30); return {'synthetic':True}
    if action not in ('metadata','download','native_probe'): raise ValueError('unknown_action')
    from .native import run_native,Budget,Stop
    bvid=payload['bvid']; part=int(payload.get('part',1)); identity(bvid)
    if not 1<=part<=1000: raise ValueError('invalid_part')
    if action=='download' and payload.get('cid'):
        prior=next((a for a in library.detail(bvid).get('assets',[]) if a['cid']==str(payload['cid'])),None)
        if prior and prior['status']=='complete' and prior['path'] and Path(prior['path']).is_file():
            if digest(prior['path'])==prior['sha256']:
                return {'download':'ALREADY_PRESENT','requests':0}
            raise ValueError('local_file_changed')
    root=Path(payload['root']).resolve()
    root.mkdir(parents=True,exist_ok=True)
    staging=root/'.bilidesk-staging'/uuid.uuid4().hex
    staging.mkdir(parents=True,exist_ok=False)
    (staging/'task.json').write_text(json.dumps({'bvid':bvid,'part':part,'cid':payload.get('cid'),
        'task_id':payload.get('task_id'),'state':'incomplete','recovery':'retain; manual retry uses a new task'},ensure_ascii=False),encoding='utf-8')
    profile=payload.get('profile') or library.setting('profile')
    if not profile: raise ValueError('dedicated_profile_required')
    ffmpeg=payload.get('ffmpeg') or library.setting('ffmpeg')
    if not ffmpeg or not Path(ffmpeg).is_file(): raise ValueError('ffmpeg_pair_required')
    budget=Budget(emit=emit)
    try:
        result=run_native(bvid=bvid,part=part,profile=profile,stage=staging,ffmpeg=ffmpeg,
                          download=action!='metadata',expected_cid=payload.get('cid'),emit=emit,budget=budget)
        work=result['work']
        if action=='metadata':
            library.save_work(work,work['parts'])
            return {'work':work,'budget':budget.summary()}
        emit({'type':'progress','phase':'核验完整音视频','progress':99,'speed':0})
        file=inspect(staging/'asset.mp4',ffmpeg,result['duration'])
        if action=='native_probe':
            return {'native':'PASS','bvid':bvid,'cid':work['cid'],'uid':work['uid'],
                    'file':file,'budget':budget.summary(),'work':work}
        library.save_work(work,work['parts'])
        category=library.detail(bvid)['category']
        destination=root/work['uid']/category/bvid/work['cid']
        destination.mkdir(parents=True,exist_ok=True)
        target=destination/'asset.mp4'
        if target.exists():
            if digest(target)!=file['sha256']: raise ValueError('destination_conflict')
        else:
            # Windows rename refuses an existing destination. Both are on the chosen volume.
            os.rename(staging/'asset.mp4',target)
        file['path']=str(target)
        library.register_file(bvid,work['cid'],file)
        preview_status='PASS'
        try:
            frames=previews(target,file['duration'],destination/('previews-'+uuid.uuid4().hex[:8]),ffmpeg)
            library.register_file(bvid,work['cid'],file,[f for f in frames if not f['duplicate']])
        except Exception:
            preview_status='FAILED_VIDEO_RETAINED'
        return {'download':'PASS','preview':preview_status,'budget':budget.summary()}
    finally:
        emit({'type':'budget',**budget.summary()})


def main():
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if stream and hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8')
    try:
        spec=json.loads(sys.stdin.readline())
        result=execute(spec)
        emit({'type':'result','result':result}); return 0
    except BaseException as exc:
        reason=getattr(exc,'reason',None)
        if not reason and isinstance(exc,ValueError) and str(exc) in {
          'report_not_passed','asset_not_verified','source_hash_mismatch','preview_source_mismatch','preview_hash_mismatch',
          'preview_missing','report_path_outside','report_link_refused','local_file_missing','local_file_changed',
          'invalid_bvid','invalid_part','dedicated_profile_required','ffmpeg_pair_required','media_timeout','media_decode_failed',
          'media_tracks_invalid','duration_mismatch','destination_conflict'}: reason=str(exc)
        emit({'type':'error','reason':reason or 'worker_operation_failed'}); return 1


if __name__=='__main__':
    raise SystemExit(main())
