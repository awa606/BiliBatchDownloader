"""Offline media validation and previews, adapted from the accepted U02/U04 checks."""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as file:
        while block:=file.read(1024*1024): h.update(block)
    return h.hexdigest()


def check_local_file(library,bvid,cid,folder=False):
    """Small offline check shared by the worker and independent local-open lane."""
    detail=library.detail(bvid)
    asset=next((a for a in detail.get('assets',[]) if str(a['cid'])==str(cid)),None)
    if not asset or not asset['path']: raise ValueError('local_file_missing')
    path=Path(asset['path'])
    try:
        if not path.is_file(): raise ValueError('local_file_missing')
        if path.stat().st_size!=asset['size']: raise ValueError('local_file_changed')
    except FileNotFoundError:
        raise ValueError('local_file_missing') from None
    return {'path':str(path),'folder':bool(folder)}


def run(command,timeout=60):
    try:
        result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:
        raise ValueError('media_timeout') from None
    if result.returncode:
        raise ValueError('media_decode_failed')
    return result


def inspect(path,ffmpeg,expected_duration=0):
    path=Path(path); ffmpeg=Path(ffmpeg)
    ffprobe=ffmpeg.with_name('ffprobe.exe' if ffmpeg.suffix=='.exe' else 'ffprobe')
    data=json.loads(run([str(ffprobe),'-v','error','-protocol_whitelist','file,pipe','-show_streams',
                         '-show_format','-of','json',str(path)],20).stdout)
    video=[s for s in data['streams'] if s.get('codec_type')=='video']
    audio=[s for s in data['streams'] if s.get('codec_type')=='audio']
    duration=float(data['format'].get('duration') or 0)
    if not video or not audio or duration<=0 or video[0]['height']>360:
        raise ValueError('media_tracks_invalid')
    if expected_duration and abs(duration-expected_duration)>max(2,expected_duration*.02):
        raise ValueError('duration_mismatch')
    run([str(ffmpeg),'-hide_banner','-nostdin','-v','error','-xerror','-protocol_whitelist','file,pipe',
         '-i',str(path),'-map','0:v:0','-map','0:a:0','-f','null','-'],120)
    return dict(path=str(path.resolve()),size=path.stat().st_size,sha256=digest(path),duration=duration,
                video_tracks=len(video),audio_tracks=len(audio),height=video[0]['height'],full_decode='PASS')


def previews(path,duration,directory,ffmpeg):
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=False)
    deadline=time.monotonic()+60; seen=set(); frames=[]
    count=min(6,max(1,math.floor(duration/5)))
    for index in range(count):
        target=duration*(index+.5)/count; output=directory/f'{index+1:02d}.png'
        result=run([str(ffmpeg),'-hide_banner','-nostdin','-v','info','-n','-ss',f'{target:.9f}','-copyts',
          '-protocol_whitelist','file,pipe','-f','mov','-i',str(path),'-map','0:v:0','-an','-sn','-dn','-vf',
          "scale=w='min(640,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease,setsar=1,showinfo",
          '-frames:v','1','-fps_mode','passthrough','-map_metadata','-1','-threads','1',str(output)],max(.01,deadline-time.monotonic()))
        match=re.search(rb'\bn:\s*0\s+pts:\s*(-?\d+)\s+pts_time:([-+0-9.eE]+)',result.stderr)
        if not match: raise ValueError('preview_pts_missing')
        seconds=float(match[2]); sha=digest(output)
        if not math.isfinite(seconds) or not target-.001<=seconds<=duration+1: raise ValueError('preview_pts_invalid')
        frame=dict(ordinal=index+1,path=str(output.resolve()),seconds=seconds,sha256=sha,
                   requested_seconds=target,duplicate=sha in seen)
        frames.append(frame); seen.add(sha)
    return frames


def child_path(root,relative):
    root=Path(root).resolve(); candidate=root/relative
    if Path(relative).is_absolute() or not candidate.resolve().is_relative_to(root):
        raise ValueError('report_path_outside')
    if any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in [candidate,*candidate.parents] if p.is_relative_to(root)):
        raise ValueError('report_link_refused')
    return candidate.resolve(strict=True)


def import_reports(library,source,preview):
    """Validate all files before registration; authorized reports only, never scan."""
    source=Path(source); preview=Path(preview)
    report=json.loads(source.read_text(encoding='utf-8')); images=json.loads(preview.read_text(encoding='utf-8'))
    if report.get('download')!='PASS' or images.get('preview')!='PASS': raise ValueError('report_not_passed')
    records=[]
    for asset in report['assets']:
        if asset.get('verification')!='PASS' or asset.get('full_decode')!='PASS': raise ValueError('asset_not_verified')
        path=child_path(source.parent,asset['relative_file'])
        if path.stat().st_size!=asset['size_bytes'] or digest(path)!=asset['sha256']: raise ValueError('source_hash_mismatch')
        frames=[]
        for item in images['assets']:
            origin=item.get('source',{})
            if origin.get('bvid')==asset['bvid'] and str(origin.get('cid'))==str(asset['cid']):
                if origin.get('sha256')!=asset['sha256']: raise ValueError('preview_source_mismatch')
                for frame in item['samples']:
                    picture=child_path(preview.parent,frame['relative_file'])
                    if digest(picture)!=frame['png_sha256']: raise ValueError('preview_hash_mismatch')
                    if frame.get('duplicate_of') is None:
                        frames.append(dict(ordinal=frame['index'],path=str(picture),seconds=frame['actual_pts_seconds'],sha256=frame['png_sha256']))
        if not frames: raise ValueError('preview_missing')
        records.append((asset,path,frames))
    for asset,path,frames in records:
        library.save_work({'bvid':asset['bvid'],'uid':str(asset['author_uid'])},
                          [{'cid':str(asset['cid']),'part':asset['part'],'duration':asset['duration_seconds']}])
        library.register_file(asset['bvid'],asset['cid'],dict(path=str(path),size=asset['size_bytes'],
            sha256=asset['sha256'],duration=asset['duration_seconds']),frames)
    return {'assets':len(records),'previews':sum(len(r[2]) for r in records),'original_files_unchanged':True}
