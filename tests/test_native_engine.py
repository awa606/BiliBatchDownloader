import gzip
import http.cookiejar
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import json
import threading
import time

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.networking.common import Request
from experiments.native_video_engine import Budget,CountedFile,observe_http,Stop,Quiet,make_extractor

@pytest.fixture
def server():
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            seen.append(dict(path=self.path,headers=dict(self.headers)))
            if self.path=='/redirect':
                self.send_response(301); self.send_header('Location','/gzip'); self.end_headers(); return
            if self.path.startswith('/status/'):
                self.send_response(int(self.path.rsplit('/',1)[1])); self.end_headers(); return
            body=gzip.compress(b'{"native":"gzip","private":"SENTINEL_SECRET"}')
            self.send_response(200); self.send_header('Content-Encoding','gzip'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=httpd.serve_forever,daemon=True); thread.start()
    yield f'http://127.0.0.1:{httpd.server_port}',seen
    httpd.shutdown(); httpd.server_close(); thread.join()

@pytest.mark.parametrize('legacy',[False,True])
def test_native_headers_redirect_and_gzip_are_unchanged(server,legacy):
    url,seen=server
    params={'quiet':True,'logger':Quiet(),'compat_opts':{'prefer-legacy-http-handler'} if legacy else set()}
    with YoutubeDL(params) as ydl:
        def fetch():
            with ydl.urlopen(Request(url+'/redirect',headers={'Referer':'https://www.bilibili.com/','Range':'bytes=0-'})) as response:
                return response.read(),response.url
        unobserved=fetch(); first=seen[:]; seen.clear()
        budget=Budget()
        with observe_http(budget): observed=fetch()
        assert observed==unobserved
        assert budget.requests==2 and budget.statuses==[301,200]
        assert seen==first
        assert seen[0]['headers']['User-Agent']
        assert seen[0]['headers']['Range']=='bytes=0-'
        assert 'SENTINEL_SECRET' not in json.dumps(budget.summary())

def test_thirteenth_request_never_sent(server):
    url,seen=server; budget=Budget()
    with YoutubeDL({'quiet':True,'logger':Quiet()}) as ydl,observe_http(budget):
        for _ in range(12):
            with ydl.urlopen(url+'/gzip') as r: r.read()
        with pytest.raises(Stop,match='request_budget'): ydl.urlopen(url+'/gzip')
    assert len(seen)==12

@pytest.mark.parametrize('status',[401,403,412,429])
def test_status_stops_without_retry(server,status):
    url,seen=server
    with YoutubeDL({'quiet':True,'logger':Quiet()}) as ydl,observe_http(Budget()):
        with pytest.raises(Stop,match='http_'+str(status)): ydl.urlopen(url+'/status/'+str(status))
    assert len(seen)==1

def test_byte_limit_and_timeout():
    budget=Budget(size=10); reader=CountedFile(io.BytesIO(b'x'*20),budget)
    assert reader.read(10)==b'x'*10
    with pytest.raises(Stop,match='transfer_size'): reader.read()
    with pytest.raises(Stop,match='worker_timeout'): Budget(seconds=-1).request()

def state(bvid='BV13x41117TL',cid=14694589):
    return dict(videoData=dict(bvid=bvid,title='synthetic',owner=dict(mid=1,name='测试'),pages=[dict(cid=cid,page=1,part='片段',duration=5)]),tags=[])

def test_actual_upstream_initial_json_parser_and_identity():
    ie=make_extractor('BV13x41117TL',1,14694589)
    with YoutubeDL({'quiet':True,'logger':Quiet()}) as ydl:
        ydl.add_info_extractor(ie)
        text='window.__INITIAL_STATE__='+json.dumps(state())+';'
        ie._search_json(r'window\.__INITIAL_STATE__\s*=',text,'initial state','BV13x41117TL')
        assert ie.observed['cid']=='14694589' and ie.observed['uid']=='1'
        assert ie.observed['tags_status']=='empty'
        with pytest.raises(Stop,match='mismatch'):
            ie._search_json(r'window\.__INITIAL_STATE__\s*=','window.__INITIAL_STATE__='+json.dumps(state(cid=2))+';','initial state','BV13x41117TL')

def test_parsed_risk_not_raw_marker():
    ie=make_extractor('BV13x41117TL',1)
    with YoutubeDL({'quiet':True,'logger':Quiet()}) as ydl:
        ydl.add_info_extractor(ie)
        with pytest.raises(Stop,match='verification_page'):
            ie._search_json(r'window\._riskdata_\s*=','window._riskdata_={"v_voucher":"SENTINEL"};','risk','test')
