"""Qt-free private drafts and group-atomic publication. Original files stay put."""
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from .gallery_engine import Stop,digest,fingerprint


def safe_path(value,file=False):
    p=Path(value)
    if not p.is_absolute() or str(p).startswith(('\\\\','//')): raise Stop('gallery_path_invalid')
    if any(x.is_symlink() or (hasattr(x,'is_junction') and x.is_junction()) for x in (p,*p.parents)):
        raise Stop('gallery_path_invalid')
    if file and not p.is_file(): raise Stop('local_file_missing')
    return p.resolve()


def checked(asset):
    p=safe_path(asset['path'],True)
    if not 0<asset['size']<=64*1024*1024 or p.stat().st_size!=asset['size'] or digest(p)!=asset['sha256']:
        raise Stop('local_file_changed')
    return p


def save_draft(library,result,root):
    ident=uuid.uuid4().hex
    result.update(draft_id=ident,root=str(safe_path(root)))
    with library.connection() as db:
        db.execute('INSERT INTO gallery_drafts VALUES(?,?,?)',(ident,json.dumps(result,ensure_ascii=False),time.time()))
    return ident


def load_draft(library,ident,sha=None):
    if not re.fullmatch('[a-f0-9]{32}',str(ident)): raise Stop('gallery_draft_invalid')
    with library.connection(True) as db:
        row=db.execute('SELECT payload FROM gallery_drafts WHERE id=?',(ident,)).fetchone()
    if not row: raise Stop('gallery_draft_invalid')
    value=json.loads(row[0])
    if value.get('attempt_active'): raise Stop('gallery_previous_attempt_unconfirmed')
    if fingerprint(value['manifest'])!=value['manifest_sha'] or sha and sha!=value['manifest_sha']:
        raise Stop('gallery_manifest_changed')
    return value


def selection(value,selected):
    if (not isinstance(selected,list) or not selected or len(set(selected))!=len(selected)
        or any(type(i)!=int or not 1<=i<=value['manifest']['group_count'] for i in selected)):
        raise Stop('gallery_selection_invalid')
    return sorted(selected)


def verify_cache(draft,selected):
    assets=[]
    for i in selected:
        a=next((a for a in draft['assets'] if a['sequence']==i and a['role']=='image'),None)
        if not a: raise Stop('gallery_cache_missing')
        p=checked(a)
        root=safe_path(draft['root'])
        if not p.is_relative_to(root/'.bilidesk-staging'): raise Stop('gallery_path_invalid')
        assets.append(a)
    return assets


def remaining_groups(library,draft,selected):
    remaining=[]
    with library.connection(True) as db:
        for number in selected:
            old=[dict(a) for a in db.execute('SELECT * FROM gallery_assets WHERE dynamic_id=? AND sequence=?',
                                           (draft['metadata']['dynamic_id'],number))]
            if not old: remaining.append(number); continue
            expected=[a for a in draft['manifest']['assets'] if a['sequence']==number]
            if {a['role'] for a in old}!={a['role'] for a in expected}: raise Stop('gallery_pair_invalid')
            for a in old:
                checked(a)
                if a['source_path_sha256']!=next(e for e in expected if e['role']==a['role'])['source_path_sha256']:
                    raise Stop('gallery_manifest_changed')
    return remaining


def save_metadata(db,result):
    m=result['metadata']; manifest=result['manifest']; ident=m['dynamic_id']
    old=db.execute('SELECT * FROM gallery_works WHERE dynamic_id=?',(ident,)).fetchone()
    if old and (old['uid']!=m['uid'] or old['group_count']!=manifest['group_count']):
        raise Stop('gallery_identity_conflict')
    for a in db.execute('SELECT sequence,role,source_path_sha256 FROM gallery_assets WHERE dynamic_id=?',(ident,)):
        match=next((r for r in manifest['assets'] if r['sequence']==a['sequence'] and r['role']==a['role']),None)
        if not match or match['source_path_sha256']!=a['source_path_sha256']: raise Stop('gallery_manifest_changed')
    db.execute('INSERT INTO authors(uid,name) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name',
               (m['uid'],m['author']))
    db.execute('''INSERT INTO gallery_works(dynamic_id,uid,title,description,published,tags,tags_status,livephoto,
      group_count,report_sha256,updated,description_status,topics,manifest,manifest_sha) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(dynamic_id) DO UPDATE SET title=excluded.title,description=excluded.description,
      published=excluded.published,tags=excluded.tags,tags_status=excluded.tags_status,updated=excluded.updated,
      description_status=excluded.description_status,topics=excluded.topics,manifest=excluded.manifest,manifest_sha=excluded.manifest_sha''',
      (ident,m['uid'],m['title'] or '动态 · '+ident,m['description'],m['published'],json.dumps(m['tags'],ensure_ascii=False),
       m['tags_status'],int(any(a['role']=='live_video' for a in manifest['assets'])),manifest['group_count'],'',time.time(),
       m['description_status'],json.dumps(m['topics'],ensure_ascii=False),json.dumps(manifest),result['manifest_sha']))


def publish(library,draft,result,selected):
    images=verify_cache(draft,selected); media=images+result['assets']; m=result['metadata']
    if result['manifest_sha']!=draft['manifest_sha']: raise Stop('gallery_manifest_changed')
    destination=safe_path(draft['root'])/m['uid']/'图文／图片'/m['dynamic_id']
    safe_path(destination); destination.mkdir(parents=True,exist_ok=True)
    published=0
    for number in selected:
        wanted=[a for a in result['manifest']['assets'] if a['sequence']==number]
        group=[a for a in media if a['sequence']==number]
        if {a['role'] for a in group}!={a['role'] for a in wanted}: raise Stop('gallery_pair_invalid')
        for a in group: checked(a)
        # Existing registered copies are retained regardless of original directory layout.
        with library.connection(True) as db:
            prior=[dict(a) for a in db.execute('SELECT * FROM gallery_assets WHERE dynamic_id=? AND sequence=?',(m['dynamic_id'],number))]
        if prior:
            if {a['role'] for a in prior}!={a['role'] for a in group}: raise Stop('destination_conflict')
            for old in prior:
                checked(old); new=next(a for a in group if a['role']==old['role'])
                if old['sha256']!=new['sha256']: raise Stop('destination_conflict')
            final=prior
        else:
            target=destination/f'{number:02}'; safe_path(target)
            if target.exists():
                if {p.name for p in target.iterdir()}!={a['filename'] for a in group}: raise Stop('destination_conflict')
                for a in group:
                    if digest(safe_path(target/a['filename'],True))!=a['sha256']: raise Stop('destination_conflict')
            else:
                staging=safe_path(draft['root'])/'.bilidesk-staging'/('publish-'+uuid.uuid4().hex)
                staging.mkdir(parents=True,exist_ok=False)
                for a in group:
                    shutil.copyfile(checked(a),staging/a['filename'])
                    if digest(staging/a['filename'])!=a['sha256']: raise Stop('local_file_changed')
                os.rename(staging,target)
            final=[a|dict(path=str(target/a['filename']),media_root=str(target),preview_path='') for a in group]
        with library.connection() as db:
            db.execute('BEGIN IMMEDIATE'); save_metadata(db,result)
            for a in final:
                db.execute('''INSERT INTO gallery_assets(dynamic_id,sequence,role,path,media_root,sha256,size,
                  source_path_sha256,format,duration,preview_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(dynamic_id,sequence,role) DO NOTHING''',
                  (m['dynamic_id'],number,a['role'],a['path'],a['media_root'],a['sha256'],a['size'],
                   a['source_path_sha256'],a['format'],a.get('duration',0),a.get('preview_path','')))
            db.execute('INSERT OR IGNORE INTO gallery_selections VALUES(?,?)',(m['dynamic_id'],number))
        published+=1
    return published
