"""Nullable paragraph text must not hide neighbouring native text fragments."""
import copy
import json
import socket

import pytest

from bilidesk.gallery_engine import run_stage
from bilidesk.gallery_metadata import metadata, MetadataStructureError
from bilidesk.gallery_transport import Guard, Stop
from test_gallery_native_probe import native, SECRET
from test_gallery_metadata_comparison import run as compare


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs): raise AssertionError('real_network_forbidden')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


def paragraphs(state):
    value = state['article']['detail']['modules'][1]['module_content']['paragraphs']
    value[0]['text'] = None  # A picture paragraph still contributes all images.
    value.extend([
        {'text': None},
        {'text': {'nodes': [{'word': {'words': ' first\n行 '}}]}},
        {'text': None},
        {'text': {'nodes': [{'word': None, 'rich': {'orig_text': '@名字', 'text': 'fallback'}},
                           {'word': {'words': 'plain'}, 'rich': {'text': '🙂'}}]}},
        {'text': None},
    ])
    return value


def test_null_text_preserves_all_adjacent_native_fragments(native):
    paragraphs(native)
    before = copy.deepcopy(native['article'])
    result = compare(native)
    assert result['adapter']['status'] == result['native']['status'] == 'PASS'
    assert result['text_fragments_equal'] is True and result['baseline'] == 'PASS'
    assert metadata(before)['description'] == ' first\n行 \n@名字plain🙂'
    assert result['adapter']['fragments']['count'] == 4
    assert len(native['calls']) == 2 and result['budget']['media_requests'] == 0
    assert native['article'] == before


def test_null_only_is_empty_without_invented_paragraphs(native):
    native['article']['detail']['modules'][1]['module_content']['paragraphs'][0]['text'] = None
    result = compare(native)
    assert result['adapter']['status'] == 'PASS' and result['text_fragments_equal'] is True
    assert metadata(native['article'])['description'] == ''
    assert metadata(native['article'])['description_status'] == 'empty'


@pytest.mark.parametrize('value', [False, 0, '', [], {}, {'nodes': None}, {'nodes': [{}]},
                                 {'nodes': [{'word': {'words': 4}}]}])
def test_null_fix_does_not_swallow_nonnull_malformed_text(native, value, caplog, capsys):
    parts = paragraphs(native)
    parts.insert(2, {'text': value})
    events = []
    with pytest.raises(Stop, match='gallery_metadata_structure_unknown'):
        run_stage('1249456774436618276', 'prepare', native['directory'], native['jar'], 'unused',
                  guard=Guard('https://www.bilibili.com/opus/1249456774436618276',
                              clock=native['time'].clock, sleep=native['time'].sleep), emit=events.append)
    assert len(native['calls']) == 2 and events[-1]['media_requests'] == 0
    assert not list(native['directory'].iterdir())
    captured = capsys.readouterr()
    assert SECRET not in json.dumps(events) + caplog.text + captured.out + captured.err


@pytest.mark.parametrize('mutation', ['drop', 'change', 'reorder', 'duplicate', 'invalid_type'])
def test_native_fragment_difference_stops_before_media(native, monkeypatch, mutation, caplog, capsys):
    from gallery_dl.extractor.bilibili import BilibiliArticleExtractor
    from gallery_dl.extractor.common import Message
    paragraphs(native)
    original = BilibiliArticleExtractor.items
    def changed(self):
        for kind, url, data in original(self):
            if kind == Message.Directory:
                data = copy.deepcopy(data)
                content = data['content']
                data['content'] = {'drop': content[:-1], 'change': [SECRET],
                                   'reorder': list(reversed(content)), 'duplicate': content + content,
                                   'invalid_type': None}[mutation]
            yield kind, url, data
    monkeypatch.setattr(BilibiliArticleExtractor, 'items', changed)
    events = []
    with pytest.raises(Stop, match='gallery_text_parity_failed'):
        run_stage('1249456774436618276', 'prepare', native['directory'], native['jar'], 'unused',
                  guard=Guard('https://www.bilibili.com/opus/1249456774436618276',
                              clock=native['time'].clock, sleep=native['time'].sleep), emit=events.append)
    assert len(native['calls']) == 2 and events[-1]['media_requests'] == 0
    assert events[0]['type'] == 'metadata_parity' and events[0]['status'] == 'FAIL'
    assert not list(native['directory'].iterdir())
    captured = capsys.readouterr()
    assert SECRET not in json.dumps(events) + caplog.text + captured.out + captured.err


def test_native_prepare_keeps_null_paragraph_images_and_complete_text(native):
    paragraphs(native)
    events = []
    result = run_stage('1249456774436618276', 'prepare', native['directory'], native['jar'], 'unused',
        guard=Guard('https://www.bilibili.com/opus/1249456774436618276',
                    clock=native['time'].clock, sleep=native['time'].sleep), emit=events.append,
        verifier=lambda *args: {'decode': 'PASS'})
    assert result['metadata']['description'] == ' first\n行 \n@名字plain🙂'
    assert len(result['assets']) == 4 and len(native['calls']) == 6
    parity = next(e for e in events if e['type'] == 'metadata_parity')
    assert parity['status'] == 'PASS' and parity['local_count'] == parity['native_count'] == 4
    assert parity['local_sha256'] == parity['native_sha256']
