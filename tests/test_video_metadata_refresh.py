import copy
import http.client
import io
import json
from types import SimpleNamespace

import pytest
from bilidesk.library import Library
from bilidesk.native import Budget,CountedFile,Stop,observe_http
from experiments.video_metadata_refresh import BVID,UID,PARTS,validate,summary


def work():
    return dict(bvid=BVID,uid=UID,author='合成作者',title='合成标题',description='PRIVATE_SENTINEL\n完整正文',
        published=1589601697,tags=['PRIVATE_TAG'],tags_status='present',
        parts=[dict(part=p,cid=c) for p,c in sorted(PARTS)]+[dict(part=3,cid='999')])


@pytest.mark.parametrize('host',['cdn.bilivideo.com','x.hdslb.com','www.bilibili.com.evil','127.0.0.1',None])
def test_no_media_host_reaches_native_request(monkeypatch,host):
    seen=[]
    monkeypatch.setattr(http.client.HTTPConnection,'putrequest',lambda *a,**kw:seen.append((a,kw)))
    budget=Budget(metadata_only=True)
    with observe_http(budget),pytest.raises(Stop,match='metadata_host_refused'):
        http.client.HTTPConnection.putrequest(SimpleNamespace(host=host),'GET','/private')
    assert seen==[] and budget.requests==0


def test_request_budget_delegates_unchanged_and_blocks_thirteenth(monkeypatch):
    seen=[]
    monkeypatch.setattr(http.client.HTTPConnection,'putrequest',lambda c,*a,**kw:seen.append((a,kw)))
    budget=Budget(metadata_only=True)
    with observe_http(budget):
        for _ in range(12):http.client.HTTPConnection.putrequest(SimpleNamespace(host='api.bilibili.com'),'GET','/x/test',skip_host=True)
        with pytest.raises(Stop,match='request_budget'):http.client.HTTPConnection.putrequest(SimpleNamespace(host='api.bilibili.com'),'GET','/x/test')
    assert len(seen)==12 and all(r==( ('GET','/x/test'),{'skip_host':True}) for r in seen)


def test_https_proxy_uses_tunnel_destination_not_proxy_address(monkeypatch):
    seen=[]
    monkeypatch.setattr(http.client.HTTPConnection,'putrequest',lambda c,*a,**kw:seen.append((a,kw)))
    conn=http.client.HTTPSConnection('local-proxy.invalid',443)
    conn.set_tunnel('api.bilibili.com',443)
    budget=Budget(metadata_only=True)
    with observe_http(budget):conn.putrequest('GET','/x/web-interface/nav',skip_host=True)
    assert seen==[(('GET','/x/web-interface/nav'),{'skip_host':True})] and budget.requests==1


def test_proxy_tunnel_to_media_is_stopped_before_dispatch(monkeypatch):
    seen=[]
    monkeypatch.setattr(http.client.HTTPConnection,'putrequest',lambda *a,**kw:seen.append(True))
    conn=http.client.HTTPSConnection('local-proxy.invalid',443)
    conn.set_tunnel('media.bilivideo.com',443)
    budget=Budget(metadata_only=True)
    with observe_http(budget),pytest.raises(Stop,match='metadata_host_refused'):conn.putrequest('GET','/asset.mp4')
    assert not seen and budget.requests==0


@pytest.mark.parametrize('status',[400,401,403,404,412,429,500,503])
def test_any_http_error_stops_metadata(status):
    class Sock:
        def makefile(self,*a,**kw):return io.BytesIO(f'HTTP/1.1 {status} Error\r\nContent-Length: 0\r\n\r\n'.encode())
    response=http.client.HTTPResponse(Sock());budget=Budget(metadata_only=True)
    with observe_http(budget),pytest.raises(Stop,match='http_'+str(status)):response.begin()
    assert budget.statuses==[status]


@pytest.mark.parametrize('field,value',[('uid','2'),('bvid','BV13x41117TL'),('description',''),('published',0),('published','123'),
                                     ('tags',[]),('tags',['']),('tags_status','pending'),('parts',[])])
def test_metadata_rejects_missing_or_wrong_identity(field,value):
    item=work();item[field]=value
    with pytest.raises(Stop):validate(item)


def test_public_summary_never_contains_text_or_tags():
    result=summary(work())
    assert result['tags_count']==1 and result['description_chars']>0
    assert 'PRIVATE' not in json.dumps(result) and '合成' not in json.dumps(result,ensure_ascii=False)


def test_network_read_failure_stops_without_raw_exception():
    class Broken:
        def read(self,*args):raise TimeoutError('PRIVATE_SENTINEL')
    with pytest.raises(Stop,match='^metadata_network_failed$'):
        CountedFile(Broken(),Budget(metadata_only=True)).read()


@pytest.fixture
def library(tmp_path):
    lib=Library(tmp_path/'library');lib.initialize()
    item=work();item.update(description='',published=0,tags=[],tags_status='pending')
    lib.save_work(item,item['parts'][:2])
    for p,c in sorted(PARTS):lib.register_file(BVID,c,dict(path=f'/synthetic/{p}.mp4',sha256='a'*64,size=1,duration=2))
    return lib


def test_refresh_preserves_files_previews_category_and_asset_count(library):
    library.classify(BVID,'投稿视频')
    before=library.detail(BVID)['assets']
    library.save_work(work(),[],refresh_existing=True)
    after=library.detail(BVID)
    assert after['assets']==before and len(after['assets'])==2
    assert after['description']==work()['description'] and after['published']==work()['published']
    assert after['tags']==work()['tags'] and after['category']=='投稿视频'


@pytest.mark.parametrize('change',['uid','part','missing','duplicate','new_asset','new_work'])
def test_failed_refresh_is_atomic(library,change):
    before=library.detail(BVID);item=copy.deepcopy(work());assets=[]
    if change=='uid':item['uid']='2'
    if change=='part':item['parts'][0]['cid']='111'
    if change=='missing':item['parts']=item['parts'][1:]
    if change=='duplicate':item['parts'].append(item['parts'][0])
    if change=='new_asset':assets=[dict(part=3,cid='999')]
    if change=='new_work':item['bvid']='BV13x41117TL'
    with pytest.raises(ValueError):library.save_work(item,assets,refresh_existing=True)
    assert library.detail(BVID)==before and library.snapshot()['total']==1
