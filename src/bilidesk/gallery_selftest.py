"""Synthetic native pipeline for the frozen worker; no socket can be opened."""
import io
import json
from pathlib import Path
import socket
import struct
import time
import zlib


def run(directory,ffmpeg):
    import requests
    from urllib3 import HTTPResponse
    from .gallery_engine import run_stage, Stop
    from .gallery_transport import Guard,NAV
    folder=Path(directory);folder.mkdir(parents=True,exist_ok=False)
    ident='1249456774436618276';page='https://www.bilibili.com/opus/'+ident
    urls=[f'https://i0.hdslb.com/bfs/{i}.png' for i in range(1,5)]
    def chunk(kind,body): return struct.pack('!I',len(body))+kind+body+struct.pack('!I',zlib.crc32(kind+body))
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',2,2,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\x00'+b'\xff\x00\x00'*2)*2))+chunk(b'IEND',b'')
    article={'id':ident,'detail':{'modules':[
        {'module_type':'author','module_author':{'mid':1337856191,'name':'synthetic','pub_ts':'1700000000'}},
        {'module_type':'content','module_content':{'paragraphs':[{'pic':{'pics':[{'url':u} for u in urls]}}]}}
    ]}}
    article['detail']['modules'][1]['module_content']['paragraphs'].extend([
        {'text':{'nodes':[{'word':None,'rich':{'text':'rich'}}]}},
        {'text':{'nodes':[{'word':{'words':'plain'},'rich':{'text':'both'}}]}}
    ])
    calls=[];old=requests.adapters.HTTPAdapter.send;connect=socket.socket.connect
    def forbidden(*args): raise RuntimeError('socket_forbidden_in_synthetic_check')
    def send(adapter,request,*args,**kwargs):
        calls.append(request.url)
        if request.url==NAV: body=b'{"code":0,"data":{"isLogin":true,"mid":1}}'
        elif request.url==page: body=('window.__INITIAL_STATE__='+json.dumps(article)+';').encode()
        elif request.url in urls: body=png
        else: raise RuntimeError('unexpected_synthetic_endpoint')
        r=requests.Response();r.url=request.url;r.status_code=200;r.encoding='utf-8'
        r.raw=HTTPResponse(body=io.BytesIO(body),status=200,preload_content=False,decode_content=False)
        return r
    requests.adapters.HTTPAdapter.send=send;socket.socket.connect=forbidden
    try:
        jar=requests.cookies.RequestsCookieJar()
        outputs=[]
        for phase in ('prepare','download'):
            destination=folder/phase;destination.mkdir()
            prior=outputs[0] if outputs else None
            g=Guard(page,prior['media_bytes'] if prior else 0,sleep=lambda _:None)
            outputs.append(run_stage(ident,phase,destination,jar,ffmpeg,prior=prior,selected=[1,3],guard=g))
        assert len(calls)==8 and outputs[1]['assets']==[]
        assert outputs[0]['metadata']['published']==1700000000
        assert outputs[0]['metadata']['description']=='rich\nplainboth'
        article['detail']['modules'].append({'module_type':'title','module_title':{'text':None}})
        destination=folder/'rejected';destination.mkdir();events=[]
        try:
            run_stage(ident,'prepare',destination,jar,ffmpeg,guard=Guard(page,sleep=lambda _:None),emit=events.append)
        except Stop as exc:
            assert exc.reason=='gallery_metadata_structure_unknown'
        else: raise AssertionError('metadata_failure_not_stopped')
        assert events[0]['type']=='metadata_diagnostic' and events[0]['field']=='title_text'
        assert len(calls)==10 and events[-1]['network_requests']==2 and events[-1]['media_requests']==0
        assert not list(destination.iterdir())
        from .gallery_diagnostic import inspect, PageGuard
        from .gallery_engine import descriptor
        baseline={'author_uid':'1337856191','assets':[]}
        for index,url in enumerate(urls,1):
            row=descriptor(url,{'num':index,'extension':'png'})
            baseline['assets'].append(dict(sequence=index,suffix='',format='png',source_path_sha256=row['source_path_sha256']))
        compared=inspect(folder,jar,baseline,guard=PageGuard(sleep=lambda _:None))
        assert compared['diagnostic']=='COMPLETED' and compared['baseline']=='PASS'
        assert compared['adapter']['diagnostic']['field']=='title_text'
        assert compared['native']['status']=='PASS' and len(calls)==12
        assert compared['budget']['media_requests']==compared['budget']['total_media_bytes']==0
        report={'status':'PASS','synthetic_requests':12,'real_network_requests':0,'native_decode':'PASS',
                'timestamp_and_text_compatibility':'PASS',
                'page_comparison':compared,
                'cache_reuse':'PASS','metadata_diagnostic':events[0],'failure_stops_before_media':'PASS'}
        (folder/'report.json').write_text(json.dumps(report),encoding='utf-8')
        return report
    finally:
        requests.adapters.HTTPAdapter.send=old;socket.socket.connect=connect
