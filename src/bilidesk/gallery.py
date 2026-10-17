"""Offline, atomic registration of native PASS reports; never a downloader."""
import hashlib
import json
import math
from pathlib import Path
import re
import time

from PySide6.QtCore import QSize, Qt, QSaveFile, QIODevice
from PySide6.QtGui import QImageReader

from .media import digest

ERRORS = frozenset(('gallery_report_invalid','gallery_report_changed','gallery_identity_conflict',
    'gallery_order_invalid','gallery_pair_invalid','gallery_limit','gallery_path_invalid',
    'local_file_missing','local_file_changed','gallery_image_invalid','gallery_cache_failed'))


def local_path(value):
    path=Path(value)
    if not path.is_absolute() or str(path).startswith(('\\\\','//')):
        raise ValueError('gallery_path_invalid')
    for part in (path,*path.parents):
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):
            raise ValueError('gallery_path_invalid')
    if not path.is_file(): raise ValueError('local_file_missing')
    return path.resolve()


def sha_value(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):
        raise ValueError('gallery_report_invalid')
    return value


def verify_file(asset):
    path=local_path(asset['path']); root=Path(asset['media_root']).resolve()
    if path.parent!=root: raise ValueError('gallery_path_invalid')
    if path.stat().st_size!=asset['size'] or digest(path)!=asset['sha256']:
        raise ValueError('local_file_changed')
    return path


def load_report(value,expected_sha=None):
    path=local_path(value)
    if path.stat().st_size>3*1024*1024: raise ValueError('gallery_limit')
    raw=path.read_bytes(); report_sha=hashlib.sha256(raw).hexdigest()
    if expected_sha and expected_sha!=report_sha: raise ValueError('gallery_report_changed')
    try:
        r=json.loads(raw.decode('utf-8'))
        live=r.get('schema_version')==2 and r.get('sample_kind')=='livephoto'
        if not (live or r.get('schema_version')==1): raise ValueError('gallery_report_invalid')
        if any(r.get(k)!='PASS' for k in ('status','baseline_match','account')) or r.get('fingerprints_unchanged') is not True:
            raise ValueError('gallery_report_invalid')
        dynamic_id=str(r['sample_dynamic_id']); uid=str(r['author_uid']); count=r['declared_images']
        if not re.fullmatch(r'[1-9][0-9]{0,24}',dynamic_id) or not re.fullmatch(r'[1-9][0-9]{0,19}',uid):
            raise ValueError('gallery_report_invalid')
        if type(count)!=int or not 1<=count<=32 or len(r['assets'])!=count*(2 if live else 1):
            raise ValueError('gallery_order_invalid')
        assets=[]; seen=set(); root=path.parent/'media'; total=0
        for i,a in enumerate(r['assets']):
            sequence=i//2+1 if live else i+1; video=live and i%2==1
            role='live_video' if video else 'image'; suffix='l' if video else ''
            fmt='mp4' if video else 'jpg' if live else 'png'
            filename=f'{sequence:02d}{suffix}.{fmt}'
            if (a['sequence'],a['suffix'],a['format'],a['filename'])!=(sequence,suffix,fmt,filename):
                raise ValueError('gallery_order_invalid')
            if live and a.get('pair_role')!=role: raise ValueError('gallery_pair_invalid')
            source_sha=sha_value(a['source_path_sha256'])
            if source_sha in seen: raise ValueError('gallery_pair_invalid')
            seen.add(source_sha)
            if a.get('download')!='PASS' or a.get('decode')!='PASS': raise ValueError('gallery_report_invalid')
            size=a['size']; duration=float(a.get('duration_seconds') or 0)
            if type(size)!=int or not 0<size<=64*1024*1024: raise ValueError('gallery_limit')
            total+=size
            if total>256*1024*1024: raise ValueError('gallery_limit')
            if video and (not math.isfinite(duration) or duration<=0 or int(a['decoded_frames'])<2):
                raise ValueError('gallery_pair_invalid')
            asset=dict(sequence=sequence,role=role,path=str(root/filename),media_root=str(root),
                sha256=sha_value(a['sha256']),size=size,source_path_sha256=source_sha,format=fmt,duration=duration)
            verify_file(asset); assets.append(asset)
        if live:
            expected=[dict(sequence=n,status='PASS',image_filename=f'{n:02d}.jpg',video_filename=f'{n:02d}l.mp4') for n in range(1,count+1)]
            if r.get('pairs')!=expected: raise ValueError('gallery_pair_invalid')
        return dict(path=str(path),report_sha256=report_sha,dynamic_id=dynamic_id,uid=uid,
                    livephoto=live,group_count=count,assets=assets)
    except (KeyError,TypeError,OverflowError,json.JSONDecodeError,UnicodeError):
        raise ValueError('gallery_report_invalid') from None


def conflict(db,report):
    old=db.execute('SELECT * FROM gallery_works WHERE dynamic_id=?',(report['dynamic_id'],)).fetchone()
    if not old: return False
    if (old['uid'],old['group_count'],bool(old['livephoto']))!=(report['uid'],report['group_count'],report['livephoto']):
        raise ValueError('gallery_identity_conflict')
    prior=[dict(r) for r in db.execute('SELECT * FROM gallery_assets WHERE dynamic_id=? ORDER BY sequence,role',(report['dynamic_id'],))]
    if len(prior)!=len(report['assets']): raise ValueError('gallery_identity_conflict')
    keys=('sequence','role','path','sha256','size','source_path_sha256','format')
    for before,after in zip(prior,report['assets']):
        if any(before[k]!=after[k] for k in keys): raise ValueError('gallery_identity_conflict')
    return True


def scan_report(library,path):
    report=load_report(path)
    with library.connection(True) as db: exists=conflict(db,report)
    return {k:report[k] for k in ('path','report_sha256','dynamic_id','uid','group_count','livephoto')} | {
        'assets':len(report['assets']),'exists':exists,'status':'READY'}


def thumbnail(library,asset):
    cache=library.directory/'cache/gallery'; cache.mkdir(parents=True,exist_ok=True)
    target=cache/(asset['sha256']+'.png')
    # Decode with a bounded allocation and dimensions. QImageReader transforms EXIF orientation.
    QImageReader.setAllocationLimit(64)
    reader=QImageReader(str(verify_file(asset))); reader.setAutoTransform(True)
    size=reader.size()
    if not size.isValid() or size.width()*size.height()>100_000_000:
        raise ValueError('gallery_image_invalid')
    if max(size.width(),size.height())>1024:
        reader.setScaledSize(size.scaled(QSize(1024,1024),Qt.KeepAspectRatio))
    image=reader.read()
    if image.isNull(): raise ValueError('gallery_image_invalid')
    output=QSaveFile(str(target))
    if not output.open(QIODevice.WriteOnly): raise ValueError('gallery_cache_failed')
    if not image.save(output,'PNG') or not output.commit(): raise ValueError('gallery_cache_failed')
    return str(target)


def import_report(library,path,expected_sha):
    if not expected_sha: raise ValueError('gallery_report_changed')
    report=load_report(path,expected_sha)
    for a in report['assets']:
        a['preview_path']=thumbnail(library,a) if a['role']=='image' else ''
    with library.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existed=conflict(db,report)
        # Recheck after cache generation and immediately before the atomic registration.
        if digest(local_path(path))!=expected_sha: raise ValueError('gallery_report_changed')
        for asset in report['assets']: verify_file(asset)
        db.execute('INSERT INTO authors(uid,name) VALUES(?,?) ON CONFLICT(uid) DO NOTHING',
                   (report['uid'],'作者待获取 · UID '+report['uid']))
        db.execute('''INSERT INTO gallery_works(dynamic_id,uid,title,livephoto,group_count,report_sha256,updated)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(dynamic_id) DO NOTHING''',
          (report['dynamic_id'],report['uid'],'标题待获取 · '+report['dynamic_id'],int(report['livephoto']),
           report['group_count'],report['report_sha256'],time.time()))
        for a in report['assets']:
            db.execute('''INSERT INTO gallery_assets(dynamic_id,sequence,role,path,media_root,sha256,size,source_path_sha256,format,duration,preview_path)
              VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dynamic_id,sequence,role) DO UPDATE SET preview_path=excluded.preview_path''',
              (report['dynamic_id'],a['sequence'],a['role'],a['path'],a['media_root'],a['sha256'],a['size'],
               a['source_path_sha256'],a['format'],a['duration'],a['preview_path']))
        db.executemany('INSERT OR IGNORE INTO gallery_selections VALUES(?,?)',
                       [(report['dynamic_id'],n) for n in range(1,report['group_count']+1)])
    return dict(status='PASS',duplicate=existed,groups=report['group_count'],assets=len(report['assets']))


def check_gallery_file(library,work_key,sequence,role,folder=False):
    if not work_key.startswith('gallery:') or role not in ('image','live_video'):
        raise ValueError('gallery_identity_conflict')
    with library.connection(True) as db:
        a=db.execute('SELECT * FROM gallery_assets WHERE dynamic_id=? AND sequence=? AND role=?',
                     (work_key[8:],sequence,role)).fetchone()
    if not a: raise ValueError('local_file_missing')
    path=verify_file(dict(a))
    return dict(path=str(path),folder=bool(folder))
