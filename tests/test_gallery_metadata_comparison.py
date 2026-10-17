"""Same synthetic response through the installed extractor and the app adapter."""
import copy
import gzip
import json
import socket

import pytest
import requests

from bilidesk.gallery_diagnostic import inspect, PageGuard, PAGE
from bilidesk.gallery_transport import Stop, NAV
from test_gallery_native_probe import native, SECRET


def run(state):
    guard = PageGuard(clock=state['time'].clock, sleep=state['time'].sleep)
    return inspect(state['directory'], state['jar'], state['baseline'], guard=guard)


def add_text(state, nodes):
    state['article']['detail']['modules'][1]['module_content']['paragraphs'].append({'text': {'nodes': nodes}})


@pytest.fixture(autouse=True)
def never_media_or_socket(monkeypatch):
    from gallery_dl import job
    def forbidden(*args, **kwargs): raise AssertionError('diagnostic_must_not_download')
    monkeypatch.setattr(job.DownloadJob, '__init__', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def test_success_uses_one_state_no_media_and_preserves_paragraphs(native, caplog, capsys):
    add_text(native, [{'word': {'words': SECRET}}])
    add_text(native, [{'rich': {'orig_text': SECRET, 'text': 'fallback'}}])
    before = copy.deepcopy(native['article'])
    result = run(native)
    assert result['diagnostic'] == 'COMPLETED' and result['baseline'] == 'PASS'
    assert result['adapter']['status'] == result['native']['status'] == 'PASS'
    assert result['text_fragments_equal'] is True
    assert result['adapter']['description']['characters'] == len(SECRET)*2+1
    assert result['budget']['network_requests'] == len(native['calls']) == 2
    assert result['budget']['media_requests'] == result['budget']['total_media_bytes'] == 0
    assert native['article'] == before and not list(native['directory'].iterdir())
    capture = capsys.readouterr()
    assert SECRET not in json.dumps(result)+caplog.text+capture.out+capture.err


@pytest.mark.parametrize('variant', ['null_word_rich', 'both'])
def test_reproduce_native_adapter_text_difference(native, variant):
    add_text(native, [{'word': None if variant=='null_word_rich' else {'words':'one'},
                       'rich': {'text':'two'}}])
    result = run(native)
    assert result['diagnostic']=='COMPLETED' and result['native']['status']=='PASS'
    if variant=='null_word_rich':
        assert result['adapter']['diagnostic']['field']=='word'
        assert result['adapter']['diagnostic']['value_type']=='NULL'
    else:
        assert result['native']['fragments']['count']==2
        assert result['adapter']['fragments']['count']==1
        assert result['text_fragments_equal'] is False


@pytest.mark.parametrize('variant,field', [('title','title_text'),('tags','tag'),('text','text')])
def test_tolerance_difference_is_not_automatically_empty(native, variant, field):
    modules=native['article']['detail']['modules']
    if variant=='title': modules.append({'module_type':'title','module_title':{'text':None}})
    elif variant=='tags': modules.append({'module_type':'title','module_title':{'tags':[{SECRET:SECRET}]}})
    else: modules[1]['module_content']['paragraphs'][0]['text']=None
    result=run(native)
    assert result['diagnostic']=='COMPLETED' and result['baseline']=='PASS'
    assert result['adapter']['diagnostic']['field']==field
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize('status',[301,412,429,500])
@pytest.mark.parametrize('endpoint',[NAV,PAGE])
def test_http_stops_before_parsers(native,status,endpoint):
    native['responses'][endpoint]=(SECRET.encode(),status,[('Location','https://invalid.test/'+SECRET)])
    result=run(native)
    assert result['diagnostic']=='NOT_COMPLETED' and result['adapter']['status']=='NOT_RUN'
    assert len(native['calls'])==(1 if endpoint==NAV else 2)
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize('variant',['login','risk','gzip_risk','gzip_invalid','utf8','duplicate','author','id','blocked','order','timeout'])
def test_stops_and_preserves_safe_evidence(native,variant):
    article=native['article'];modules=article['detail']['modules']
    if variant=='login': native['responses'][NAV]=(b'{"code":-101}',)
    elif variant=='risk': native['responses'][PAGE]=(b'window._riskdata_='+SECRET.encode(),)
    elif variant=='gzip_risk': native['responses'][PAGE]=(gzip.compress(b'window.__riskdata__ = {}'),200,[('Content-Encoding','gzip')])
    elif variant=='gzip_invalid': native['responses'][PAGE]=(gzip.compress(b'text')[:-3],200,[('Content-Encoding','gzip')])
    elif variant=='utf8': native['responses'][PAGE]=(b'\xff',)
    elif variant=='duplicate':
        body='window.__INITIAL_STATE__='+json.dumps(article)+';'
        native['responses'][PAGE]=(body.replace('"module_blocked": null','"module_blocked":null,"module_blocked":null').encode(),)
    elif variant=='author': modules[0]['module_author']['mid']=42
    elif variant=='id': article['id']='42'
    elif variant=='blocked': modules[1]['module_blocked']={'hint_message':SECRET}
    elif variant=='order': modules[1]['module_content']['paragraphs'][0]['pic']['pics'].reverse()
    else: native['responses'][PAGE]=requests.Timeout(SECRET)
    result=run(native)
    assert result['diagnostic']=='NOT_COMPLETED' and 'stop_reason' in result
    assert result['budget']['network_requests']<=2 and result['budget']['media_requests']==0
    assert SECRET not in json.dumps(result)


def test_third_request_denied_even_if_allowed_list_is_changed(native):
    from gallery_dl.extractor.bilibili import BilibiliArticleExtractor
    from bilidesk.gallery_transport import native_configuration
    g=PageGuard(clock=native['time'].clock,sleep=native['time'].sleep)
    with native_configuration(native['directory']):
        ex=BilibiliArticleExtractor.from_url(PAGE);ex.initialize();g.install(ex.session)
        try:
            ex.request(NAV);g.account_ok=True;ex.request(PAGE)
            g.allowed=native['urls'];g.sample.assets=4;g.sample.requests=6
            with pytest.raises(Stop,match='request_budget_exhausted'): ex.request(native['urls'][0])
            assert len(native['calls'])==2
        finally:g.close();ex.session.close()


def test_gzip_preserves_raw_and_decoded_summaries(native):
    plain=('window.__INITIAL_STATE__='+json.dumps(native['article'])+';').encode()
    native['responses'][PAGE]=(gzip.compress(plain),200,[('Content-Encoding','gzip')])
    result=run(native)
    page=result['budget']['requests'][1]
    assert page['raw_sha256']!=page['decoded_sha256'] and page['decoded_bytes']==len(plain)
    assert result['diagnostic']=='COMPLETED'


def test_deadline_is_sixty_seconds(native):
    g=PageGuard(clock=native['time'].clock,sleep=native['time'].sleep)
    native['time'].now=60
    result=inspect(native['directory'],native['jar'],native['baseline'],guard=g)
    assert result['stop_reason']=='worker_total_timeout' and not native['calls']
