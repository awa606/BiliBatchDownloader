"""Separate Qt-free native gallery worker; one explicit bounded stage per process."""
import json
import os
from pathlib import Path
import sys
import uuid

from .gallery_engine import Stop,canonical,run_stage
from .gallery_store import safe_path,save_draft,load_draft,selection,verify_cache,publish,remaining_groups,save_metadata
from .gallery_transport import load_bili_cookies
from .library import Library


def execute(spec,emit):
    library=Library(spec['data_dir']); action=spec['action']; payload=spec.get('payload',{})
    if action not in ('gallery_prepare','gallery_download'): raise Stop('unknown_action')
    if library.setting('offline','0')=='1' or library.setting('gallery_online','0')!='1':
        raise Stop('offline_mode')
    if library.setting('online_stopped','0')=='1': raise Stop('online_stopped')
    profile=library.setting('profile'); ffmpeg=library.setting('ffmpeg')
    if not profile: raise Stop('dedicated_profile_required')
    if not ffmpeg or not Path(ffmpeg).is_file() or not Path(ffmpeg).with_name('ffprobe.exe').is_file():
        raise Stop('ffmpeg_pair_required')
    prior=None; selected=[]
    if action=='gallery_prepare':
        dynamic_id=canonical(payload['url']); root=safe_path(library.setting('root'))
    else:
        prior=load_draft(library,payload['draft_id'],payload['manifest_sha'])
        selected=selection(prior,payload['selected'])
        selected=remaining_groups(library,prior,selected)
        if not selected:
            return dict(download='ALREADY_PRESENT',groups=0,requests=0,work_key='gallery:'+prior['metadata']['dynamic_id'])
        verify_cache(prior,selected)
        dynamic_id=prior['metadata']['dynamic_id']; root=safe_path(prior['root'])
    root.mkdir(parents=True,exist_ok=True)
    stage=root/'.bilidesk-staging'/uuid.uuid4().hex; safe_path(stage); stage.mkdir(parents=True,exist_ok=False)
    (stage/'task.json').write_text(json.dumps(dict(action=action,dynamic_id=dynamic_id,selected=selected,
        task_id=payload.get('task_id'),status='INCOMPLETE')),encoding='utf-8')
    jar,_=load_bili_cookies(profile,None)
    def persist(total=None,active=None):
        if prior is None: return
        if total is not None: prior['media_bytes']=total
        if active is not None: prior['attempt_active']=active
        with library.connection() as db:
            db.execute('UPDATE gallery_drafts SET payload=? WHERE id=?',
                (json.dumps(prior,ensure_ascii=False),prior['draft_id']))
    persist(active=True)
    def record(event):
        if event.get('type')=='budget':
            (stage/'budget.json').write_text(json.dumps(event,indent=2),encoding='utf-8')
        emit(event)
    def manifest(value):
        if prior is None: return
        with library.connection() as db:
            db.execute('BEGIN IMMEDIATE');save_metadata(db,value)
            db.executemany('INSERT OR IGNORE INTO gallery_selections VALUES(?,?)',[(dynamic_id,n) for n in selected])
    try:
        result=run_stage(dynamic_id,'prepare' if action=='gallery_prepare' else 'download',stage,jar,ffmpeg,
                         prior=prior,selected=selected,emit=record,baseline=spec.get('baseline'),checkpoint=persist,on_manifest=manifest)
    finally:
        persist(active=False)
    if action=='gallery_prepare':
        draft_id=save_draft(library,result,root)
        return dict(draft_id=draft_id,manifest_sha=result['manifest_sha'],groups=result['manifest']['group_count'])
    count=publish(library,prior,result,selected)
    return dict(download='PASS',groups=count,work_key='gallery:'+dynamic_id)


def main():
    if len(sys.argv)==4 and sys.argv[1]=='--offline-self-test':
        from .gallery_selftest import run
        print(json.dumps(run(sys.argv[2],sys.argv[3]))); return 0
    for stream in (sys.stdin,sys.stdout):
        if stream and hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8')
    output=sys.stdout
    def emit(value):
        output.write(json.dumps(value,ensure_ascii=False)+'\n'); output.flush()
    # Native libraries must never leak raw stderr into the parent or terminal.
    quiet=os.open(os.devnull,os.O_WRONLY); os.dup2(quiet,2); os.close(quiet)
    try:
        raw=sys.stdin.readline(1024*1024+1)
        if len(raw)>1024*1024: raise Stop('worker_input_limit')
        result=execute(json.loads(raw),emit); emit({'type':'result','result':result}); return 0
    except Stop as exc:
        emit({'type':'error','reason':exc.reason}); return 1
    except BaseException:
        emit({'type':'error','reason':'gallery_operation_failed'}); return 1


if __name__=='__main__': raise SystemExit(main())
