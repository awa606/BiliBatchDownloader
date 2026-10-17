from pathlib import Path
import copy
import sys
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from bilidesk.gallery_engine import run_stage,canonical,Stop
from bilidesk.gallery_transport import Guard
from test_gallery_native_probe import native,live,real_live_media,SECRET


def run(state,phase='prepare',prior=None,selected=()):
    folder=state['directory']/phase; folder.mkdir()
    guard=Guard('https://www.bilibili.com/opus/1249456774436618276',
                prior['media_bytes'] if prior else 0,clock=state['time'].clock,sleep=state['time'].sleep)
    events=[]
    result=run_stage('1249456774436618276',phase,folder,state['jar'],'unused',prior=prior,selected=selected,
                     guard=guard,verifier=lambda p,d:{'decode':'PASS'},emit=events.append)
    assert SECRET not in str(events)
    return result


def test_two_native_phases_cache_pictures_and_select_videos(native):
    pics=native['article']['detail']['modules'][1]['module_content']['paragraphs'][0]['pic']['pics']
    for i,pic in enumerate(pics,1):
        pic['live_url']=f'https://i0.hdslb.com/bfs/{i}.mp4'
        native['media'][pic['live_url']]=b'synthetic-not-real-decode'
    prepared=run(native)
    assert prepared['budget']['network_requests']==6 and len(prepared['assets'])==4
    downloaded=run(native,'download',prepared,[1,3])
    assert downloaded['budget']['network_requests']==4 and len(downloaded['assets'])==2
    assert [a['sequence'] for a in downloaded['assets']]==[1,3]
    assert not any(c['url']==pics[1]['live_url'] for c in native['calls'])


def test_four_png_confirmation_requests_no_media(native):
    prior=run(native)
    result=run(native,'download',prior,[1,3])
    assert result['budget']['network_requests']==2 and result['assets']==[]


@pytest.mark.parametrize('change',['author','count','order','blocked','unknown_tag'])
def test_stop_before_media_on_changed_or_unsupported_page(native,change):
    prior=run(native); native['calls'].clear()
    modules=native['article']['detail']['modules']
    if change=='author': modules[0]['module_author']['mid']=42
    elif change=='count': modules[1]['module_content']['paragraphs'][0]['pic']['pics'].pop()
    elif change=='order': modules[1]['module_content']['paragraphs'][0]['pic']['pics'].reverse()
    elif change=='blocked': modules[1]['module_blocked']={'hint_message':SECRET}
    else: modules.append({'module_type':'title','module_title':{'tags':[{'badge':SECRET}]}})
    with pytest.raises(Stop): run(native,'download',prior,[1])
    assert len(native['calls'])==2


@pytest.mark.parametrize('status',[301,403,412,429,500])
def test_first_failure_stops(native,status):
    native['responses'][native['urls'][0]]=(b'',status)
    with pytest.raises(Stop): run(native)
    assert len(native['calls'])==3


@pytest.mark.parametrize('url',['https://evil.test/opus/123','http://www.bilibili.com/opus/123',
    'https://www.bilibili.com:443/opus/123','https://www.bilibili.com/opus/123#x'])
def test_input_scope(url):
    with pytest.raises(Stop): canonical(url)


def test_input_tracking_query_removed_without_fetch():
    assert canonical('https://t.bilibili.com/123?spm_id_from=abc')=='123'


@pytest.mark.parametrize('variant',['valid','oneframe','corrupt'])
def test_selected_livephoto_real_decode(live,real_live_media,variant):
    import time
    for i,url in enumerate(live['urls']):
        live['media'][url]=real_live_media['photo.jpg' if i%2==0 else 'video.mp4']
    ident='1257912749013336118'
    def phase(name,prior=None):
        folder=live['directory']/name;folder.mkdir()
        g=Guard('https://www.bilibili.com/opus/'+ident,prior['media_bytes'] if prior else 0,
                clock=live['time'].clock,sleep=live['time'].sleep)
        g.deadline=time.monotonic()+180
        return run_stage(ident,name,folder,live['jar'],real_live_media['ffmpeg'],guard=g,prior=prior,selected=[1,5,9])
    prior=phase('prepare'); assert prior['budget']['network_requests']==11
    assert len(prior['assets'])==9
    if variant=='oneframe': live['media'][live['urls'][1]]=real_live_media['oneframe.mp4']
    if variant=='corrupt': live['media'][live['urls'][1]]=b'bad'
    if variant!='valid':
        with pytest.raises(Stop): phase('download',prior)
        assert len(live['calls'])==14
    else:
        result=phase('download',prior)
        assert result['budget']['network_requests']==5
        assert [a['sequence'] for a in result['assets']]==[1,5,9]
        assert all(a['decode']['decoded_frames']==3 for a in result['assets'])
        assert len(live['calls'])==16
        assert all(live['urls'][i] not in [c['url'] for c in live['calls']] for i in [3,5,7,11,13,15])


def test_new_guard_preserves_native_request_semantics(native,tmp_path):
    from gallery_dl import job
    from gallery_dl.extractor.bilibili import BilibiliArticleExtractor
    from bilidesk.gallery_transport import native_configuration,NAV
    run(native);observed=copy.deepcopy(native['calls']);native['calls'].clear()
    with native_configuration(tmp_path/'plain'):
        ex=BilibiliArticleExtractor.from_url('https://www.bilibili.com/opus/1249456774436618276')
        ex.initialize();ex.session.cookies.update(native['jar']);ex.request(NAV)
        task=job.DownloadJob(ex);task.run();ex.session.close()
        assert task.status==0
    for guarded,plain in zip(observed,native['calls'],strict=True):
        assert {k:v for k,v in guarded.items() if k!='at'}=={k:v for k,v in plain.items() if k!='at'}


@pytest.mark.parametrize('variant',['group_limit','duplicate','gzip_risk'])
def test_product_limits_before_media(native,variant):
    import json,gzip
    modules=native['article']['detail']['modules']
    if variant=='group_limit': modules[1]['module_content']['paragraphs'][0]['pic']['pics']*=9
    elif variant=='duplicate': modules[1]['module_content']['paragraphs'][0]['pic']['pics'][1]=modules[1]['module_content']['paragraphs'][0]['pic']['pics'][0]
    else: native['responses']['https://www.bilibili.com/opus/1249456774436618276']=(gzip.compress(b'window.__riskdata__ = {}'),200,[('Content-Encoding','gzip')])
    with pytest.raises(Stop):run(native)
    assert len(native['calls'])==2
