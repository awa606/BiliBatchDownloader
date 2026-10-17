"""Isolated candidate: native yt-dlp flow with observation and hard stop budgets.

No urlopen override, replacement HTTP client, manual media download or merge.
The low-level observation is installed only inside a dedicated serial worker.
"""
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.cookies import extract_cookies_from_browser
from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.version import __version__


class Stop(BaseException):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Quiet:
    def debug(self,*args,**kwargs): pass
    info=debug
    warning=debug
    error=debug


class Budget:
    def __init__(self, requests=12, seconds=230, size=64*1024*1024, emit=lambda e: None, metadata_only=False):
        self.limit=requests; self.deadline=time.monotonic()+seconds; self.size=size
        self.requests=0; self.bytes=0; self.statuses=[]; self.emit=emit
        self.metadata_only=metadata_only

    def check(self):
        if time.monotonic()>self.deadline:
            raise Stop('worker_timeout')

    def request(self,host=None):
        self.check()
        if self.metadata_only and host not in ('www.bilibili.com','api.bilibili.com'):
            raise Stop('metadata_host_refused')
        if self.requests>=self.limit:
            raise Stop('request_budget_exhausted')
        self.requests+=1
        self.emit({'type':'budget','requests':self.requests,'wire_bytes':self.bytes})

    def summary(self):
        return {'requests':self.requests,'wire_bytes':self.bytes,'http_statuses':self.statuses}


class CountedFile:
    def __init__(self,source,budget):
        self.source=source; self.budget=budget

    def __getattr__(self,key):
        return getattr(self.source,key)

    def _read(self,method,size=-1):
        self.budget.check()
        remaining=self.budget.size-self.budget.bytes
        limit=remaining+1 if size is None or size<0 else min(size,remaining+1)
        try:
            data=getattr(self.source,method)(limit)
        except Exception:
            if self.budget.metadata_only: raise Stop('metadata_network_failed') from None
            raise
        self.budget.bytes+=len(data)
        if self.budget.bytes>self.budget.size:
            raise Stop('transfer_size_limit')
        return data

    def read(self,size=-1): return self._read('read',size)
    def readline(self,size=-1): return self._read('readline',size)
    def read1(self,size=-1): return self._read('read1',size)
    def readinto(self,buffer):
        data=self.read(len(buffer)); buffer[:len(data)]=data; return len(data)


@contextmanager
def observe_http(budget):
    """Delegate unchanged; count physical requests including redirects and retries.

    All HTTP wire bytes (including metadata and headers) consume the ceiling,
    which is stricter than a media-only ceiling. No request headers are touched.
    """
    original_request=http.client.HTTPConnection.putrequest
    original_begin=http.client.HTTPResponse.begin
    def request(connection,*args,**kwargs):
        # HTTPS proxies keep their own address in host; the origin is the tunnel target.
        target=getattr(connection,'_tunnel_host',None) or connection.host
        budget.request(target)
        try:
            return original_request(connection,*args,**kwargs)
        except Exception:
            if budget.metadata_only: raise Stop('metadata_network_failed') from None
            raise
    def begin(response,*args,**kwargs):
        if response.headers is None and not isinstance(response.fp,CountedFile):
            response.fp=CountedFile(response.fp,budget)
        try:
            result=original_begin(response,*args,**kwargs)
        except Exception:
            if budget.metadata_only: raise Stop('metadata_network_failed') from None
            raise
        budget.statuses.append(response.status)
        if response.status in (401,403,412,429) or budget.metadata_only and response.status>=400:
            raise Stop('http_'+str(response.status))
        return result
    http.client.HTTPConnection.putrequest=request
    http.client.HTTPResponse.begin=begin
    try:
        yield
    finally:
        http.client.HTTPConnection.putrequest=original_request
        http.client.HTTPResponse.begin=original_begin


def cookies(profile):
    try:
        source=extract_cookies_from_browser('chrome',profile,logger=Quiet())
        for cookie in list(source):
            domain=cookie.domain.lstrip('.').lower()
            if not (domain=='bilibili.com' or domain.endswith('.bilibili.com')) or cookie.is_expired():
                source.clear(cookie.domain,cookie.path,cookie.name)
        if not any(c.name=='SESSDATA' and c.value for c in source):
            raise Stop('session_cookie_unavailable')
        return source
    except Exception:
        raise Stop('cookie_load_failed') from None


def make_extractor(bvid,part,expected_cid=None):
    class IdentityIE(BiliBiliIE):
        observed=None

        def _search_json(self,*args,**kwargs):
            data=super()._search_json(*args,**kwargs)
            if len(args)>2 and args[2]=='risk' and isinstance(data,dict) and data.get('v_voucher'):
                raise Stop('verification_page')
            if len(args)>2 and args[2]=='initial state':
                video=data.get('videoData',{}) if isinstance(data,dict) else {}
                pages=video.get('pages',[])
                if video.get('bvid')!=bvid or not video.get('title') or len(pages)<part:
                    raise Stop('identity_missing_or_mismatch')
                page=pages[part-1]
                owner=video.get('owner') or data.get('upData') or {}
                cid=page.get('cid')
                if not cid or expected_cid and str(cid)!=str(expected_cid):
                    raise Stop('cid_mismatch')
                if not str(owner.get('mid','')).isdigit():
                    raise Stop('author_identity_missing')
                self.observed={'bvid':bvid,'uid':str(owner['mid']),'author':str(owner.get('name') or ''),
                  'title':video['title'],'description':video.get('desc',''),'published':video.get('pubdate',0),
                  'cid':str(cid),'part':part,'parts':[{'cid':str(p['cid']),'part':int(p['page']),
                    'title':p.get('part',''),'duration':float(p.get('duration') or 0)} for p in pages],
                  'tags':[str(t['tag_name']) for t in data.get('tags',[]) if isinstance(t,dict) and 'tag_name' in t],
                  'tags_status':'present' if data.get('tags') else ('empty' if 'tags' in data else 'pending')}
            return data

        def _download_json(self,*args,**kwargs):
            value=super()._download_json(*args,**kwargs)
            if isinstance(value,dict) and value.get('code') in (-101,-111,-352,-401,-412,22115):
                raise Stop('platform_api_stop')
            return value
    return IdentityIE()


def run_native(*,bvid,part,profile,stage,ffmpeg,download,expected_cid=None,emit=lambda e:None,budget=None):
    if __version__!='2026.08.19':
        raise Stop('dependency_version_changed')
    stage=Path(stage)
    budget=budget or Budget(emit=emit)
    jar=cookies(profile)
    extractor=make_extractor(bvid,part,expected_cid)
    def progress(event):
        budget.check()
        emit({'type':'progress','phase':'下载中' if event['status']=='downloading' else '准备合并',
              'progress':min(99,100*event.get('downloaded_bytes',0)/max(1,event.get('total_bytes') or event.get('total_bytes_estimate') or 1)),
              'speed':float(event.get('speed') or 0)})
    params={'quiet':True,'no_warnings':True,'logger':Quiet(),'cachedir':False,'socket_timeout':15,
      'retries':0,'fragment_retries':0,'extractor_retries':0,'file_access_retries':0,'noplaylist':True,
      'concurrent_fragment_downloads':1,'writethumbnail':False,'writeinfojson':False,'writesubtitles':False,
      'outtmpl':str(stage/'asset.%(ext)s'),'ffmpeg_location':str(ffmpeg),'merge_output_format':'mp4',
      'format':'bestvideo[height<=360][ext=mp4][protocol=https]+bestaudio[ext=m4a][protocol=https]/best[height<=360][ext=mp4][protocol=https]',
      'progress_hooks':[progress],'postprocessor_hooks':[lambda e:emit({'type':'progress','phase':'合并音视频','progress':99,'speed':0})],
      'overwrites':False,'continuedl':True,'max_filesize':64*1024*1024,'skip_unavailable_fragments':False}
    with YoutubeDL(params,auto_init=False) as ydl:
        ydl.cookiejar=jar
        ydl.add_info_extractor(extractor)
        handlers=set(ydl._request_director.handlers)
        if handlers-{'Urllib','Requests'}:
            raise Stop('unobserved_network_handler')
        with observe_http(budget):
            emit({'type':'progress','phase':'检查登录','progress':0,'speed':0})
            with ydl.urlopen('https://api.bilibili.com/x/web-interface/nav') as response:
                nav=json.loads(response.read())
            if nav.get('code')!=0 or not nav.get('data',{}).get('isLogin'):
                raise Stop('login_expired')
            emit({'type':'progress','phase':'读取作品','progress':0,'speed':0})
            info=ydl.extract_info(f'https://www.bilibili.com/video/{bvid}?p={part}',download=download,ie_key=extractor.ie_key())
        if not extractor.observed or not info or not str(info.get('id','')).startswith(bvid):
            raise Stop('native_result_identity_missing')
        if download and not (stage/'asset.mp4').is_file():
            raise Stop('native_file_missing')
        return {'work':extractor.observed,'duration':float(info.get('duration') or 0),'budget':budget.summary()}
