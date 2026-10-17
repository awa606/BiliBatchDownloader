"""Pinned native gallery-dl engine. No Qt, database, raw logs or URL persistence."""
import copy
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .gallery_transport import (Stop, Guard, NAV, native_configuration, audit_state,
                                original_modules, inspect_asset)
from .gallery_metadata import metadata, author_fields, MetadataStructureError

PINS = {'gallery-dl':'1.32.16','requests':'2.34.2','urllib3':'2.8.0'}


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def canonical(value):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 for c in value):
        raise Stop('gallery_url_invalid')
    p=urlsplit(value)
    if p.scheme!='https' or p.username or p.password or p.port or p.fragment:
        raise Stop('gallery_url_invalid')
    match = re.fullmatch(r'/opus/([1-9][0-9]{0,24})/?',p.path) if p.netloc=='www.bilibili.com' else (
        re.fullmatch(r'/([1-9][0-9]{0,24})/?',p.path) if p.netloc=='t.bilibili.com' else None)
    if not match: raise Stop('gallery_url_invalid')
    return match[1]


def descriptor(url,data):
    p=urlsplit(url); fmt=data.get('extension'); suffix=data.get('suffix','')
    video=suffix=='l' and fmt=='mp4'; host=p.hostname or ''
    host_ok=host in ('i0.hdslb.com','i1.hdslb.com','i2.hdslb.com') or (
        video and any(host.endswith('.'+s) for s in ('bilivideo.com','bilivideo.cn')) and
        all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',v) for v in host.split('.')))
    if (len(url)>8192 or any(ord(c)<33 for c in url) or p.scheme not in ('http','https') or not host_ok
        or p.username or p.password or p.fragment or p.port not in (None,80 if p.scheme=='http' else 443)
        or (not video and (suffix or fmt not in ('png','jpg','jpeg') or not p.path.startswith('/bfs/')))
        or not p.path.endswith('.'+str(fmt)) or type(data.get('num'))!=int or not 1<=data['num']<=32):
        raise Stop('media_endpoint_or_format_rejected')
    return dict(sequence=data['num'],role='live_video' if video else 'image',format=fmt,
                source_path_sha256=hashlib.sha256(p.path.encode()).hexdigest(),
                filename=f"{data['num']:02}{suffix}.{fmt}")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def run_stage(dynamic_id,phase,directory,jar,ffmpeg,*,prior=None,selected=(),emit=lambda e:None,
              guard=None,verifier=None,baseline=None,checkpoint=lambda value:None,on_manifest=lambda value:None):
    from gallery_dl import job
    from gallery_dl.extractor.bilibili import BilibiliArticleExtractor
    from gallery_dl.extractor.common import Message
    if not re.fullmatch(r'[1-9][0-9]{0,24}',str(dynamic_id)) or phase not in ('prepare','download'):
        raise Stop('gallery_input_invalid')
    if any(version(k)!=v for k,v in PINS.items()): raise Stop('gallery_dependency_changed')
    page='https://www.bilibili.com/opus/'+dynamic_id
    guard=guard or Guard(page, prior.get('media_bytes',0) if prior else 0)
    guard.checkpoint=checkpoint
    extractor=None; result={}; saved=[]
    try:
        with native_configuration(directory):
            extractor=BilibiliArticleExtractor.from_url(page); extractor.initialize()
            extractor.session.cookies.update(jar); guard.install(extractor.session)
            nav=extractor.request(NAV).json(); account=nav.get('data')
            if (nav.get('code')!=0 or not isinstance(account,dict) or account.get('isLogin') is not True
                or type(account.get('mid'))!=int or account['mid']<=0): raise Stop('login_expired')
            if any(c.value!=str(account['mid']) for c in jar if c.name=='DedeUserID'):
                raise Stop('account_cookie_identity_mismatch')
            guard.account_ok=True
            original_request=extractor.request
            def request(url,*args,**kwargs):
                if url!=page: raise Stop('unexpected_source_endpoint')
                response=original_request(url,*args,**kwargs)
                audit=audit_state(response.content.decode('utf-8',errors='strict'))
                if audit['status']!='PARSED' or audit['assignment_count']!=1 or audit['duplicate_member_count']:
                    raise Stop('page_state_ambiguous_or_invalid')
                return response
            extractor.request=request
            original_article=extractor.api.article
            def article(ident):
                value=original_article(ident); structure=original_modules(value)
                if str(value.get('id'))!=dynamic_id: raise Stop('work_identity_mismatch')
                if structure['status']!='OBSERVED': raise Stop('original_modules_invalid')
                if any(f['truthy'] or f['value_type'] not in ('NULL','OBJECT') for f in structure['blocked_fields']):
                    raise Stop('effective_blocked_module')
                authors=[m['module_author'] for m in value['detail']['modules'] if m.get('module_author')]
                if not authors or any(str(a.get('mid'))!=str(authors[0].get('mid')) for a in authors):
                    raise Stop('author_identity_mismatch')
                author=authors[0]; uid=str(author.get('mid'))
                if not re.fullmatch('[1-9][0-9]{0,19}',uid): raise Stop('author_identity_mismatch')
                try:
                    result.update(metadata(value))
                    name, published = author_fields(author)
                except MetadataStructureError as exc:
                    emit(exc.diagnostic)
                    raise Stop('gallery_metadata_structure_unknown') from None
                result.update(dynamic_id=dynamic_id,uid=uid,author=name,published=published)
                if len(json.dumps(result,ensure_ascii=False).encode())>256*1024:
                    raise Stop('gallery_metadata_limit')
                return value
            extractor.api.article=article
            messages=[]; rows=[]; urls=[]; count=None
            for kind,url,data in extractor.items():
                data=copy.deepcopy(data)
                if str(data.get('id'))!=dynamic_id or str(data.get('user_id'))!=result['uid']:
                    raise Stop('message_identity_mismatch')
                if any(k.startswith('_http') for k in data): raise Stop('message_control_rejected')
                if kind==Message.Directory and not messages:
                    count=data.get('count')
                    if type(count)!=int or not 1<=count<=32: raise Stop('gallery_group_limit')
                elif kind==Message.Url and messages:
                    row=descriptor(url,data); rows.append(row); urls.append(url)
                else: raise Stop('message_contract_changed')
                messages.append((kind,url,data))
                if len(messages)>65: raise Stop('gallery_group_limit')
            expected=[]
            for i in range(1,(count or 0)+1):
                group=[r for r in rows if r['sequence']==i]
                if [r['role'] for r in group] not in (['image'],['image','live_video']):
                    raise Stop('gallery_pair_invalid')
                expected.extend(group)
            if rows!=expected or not rows or len(set(urls))!=len(rows) or len({r['source_path_sha256'] for r in rows})!=len(rows):
                raise Stop('gallery_order_invalid')
            manifest={'dynamic_id':dynamic_id,'uid':result['uid'],'group_count':count,'assets':rows}
            manifest_sha=fingerprint(manifest)
            if prior and (prior['manifest_sha']!=manifest_sha or prior['manifest']!=manifest):
                raise Stop('gallery_manifest_changed')
            if baseline:
                old=[dict(sequence=r['sequence'],suffix='l' if r['role']=='live_video' else '',
                          format=r['format'],source_path_sha256=r['source_path_sha256']) for r in rows]
                if old!=baseline['assets'] or result['uid']!=baseline['author_uid']:
                    raise Stop('fixed_sample_baseline_mismatch')
            if phase=='download' and (not prior or not selected or any(type(i)!=int or not 1<=i<=count for i in selected)
                                      or len(set(selected))!=len(selected)):
                raise Stop('gallery_selection_invalid')
            wanted=[i for i,r in enumerate(rows) if (phase=='prepare' and r['role']=='image') or
                    (phase=='download' and r['role']=='live_video' and r['sequence'] in selected)]
            guard.allowed=[urls[i] for i in wanted]; guard.formats=[rows[i]['format'] for i in wanted]
            guard.sample.assets=len(wanted); guard.sample.requests=2+len(wanted)
            on_manifest(dict(metadata=result,manifest=manifest,manifest_sha=manifest_sha))
            filtered=[messages[0]]+[messages[i+1] for i in wanted]
            extractor.items=lambda:iter(filtered)
            emit({'type':'progress','phase':'读取预览' if phase=='prepare' else '下载所选实况','progress':0,'speed':0})
            class SelectedJob(job.DownloadJob):
                def handle_url(self,url,data):
                    row=descriptor(url,data); super().handle_url(url,data)
                    if self.status: raise Stop('native_download_failed')
                    path=directory/row['filename']; event=guard.trace[-1]
                    if (not path.is_file() or path.is_symlink() or path.is_junction() or
                        event.get('complete') is not True or event['decoded_bytes']!=path.stat().st_size or
                        event['decoded_sha256']!=digest(path)): raise Stop('local_file_transport_mismatch')
                    checked=(verifier(path,guard.deadline) if verifier else
                             inspect_asset(path,row['format'],ffmpeg,Path(ffmpeg).with_name('ffprobe.exe'),guard.deadline))
                    if checked.get('decode')!='PASS': raise Stop('local_decode_failed')
                    saved.append(row|dict(path=str(path),size=path.stat().st_size,sha256=digest(path),
                                          duration=checked.get('duration_seconds') or 0,decode=checked))
                    emit({'type':'progress','phase':'核验媒体','progress':len(saved)*100/max(1,len(wanted)),
                          'speed':guard.total_media/max(.001,guard.clock()-guard.started)})
            task=SelectedJob(extractor); task.run()
            if task.status or len(saved)!=len(wanted) or guard.count!=guard.sample.requests:
                raise Stop('native_task_incomplete')
            return dict(metadata=result,manifest=manifest,manifest_sha=manifest_sha,assets=saved,
                        media_bytes=guard.total_media,budget=guard.summary())
    finally:
        emit({'type':'budget',**guard.summary()})
        guard.close()
        if extractor: extractor.session.close()
