"""Field diagnostics use categories, never values or arbitrary source keys."""
import json

import pytest

from bilidesk.gallery_metadata import metadata, author_fields, MetadataStructureError
from bilidesk.gallery_engine import run_stage, Stop
from bilidesk.gallery_transport import Guard
from test_gallery_native_probe import native, SECRET


@pytest.mark.parametrize('module,field,kind', [
    ({'module_title': SECRET}, 'title', 'STRING'),
    ({'module_title': {'text': None}}, 'title_text', 'NULL'),
    ({'module_title': {'tags': SECRET}}, 'tags', 'STRING'),
    ({'module_title': {'tags': [{SECRET: SECRET}]}}, 'tag', 'OBJECT'),
    ({'module_topic': SECRET}, 'topic', 'STRING'),
    ({'module_topic': {SECRET: SECRET}}, 'topic_name', 'MISSING'),
    ({'module_topic': {'name': False}}, 'topic_name', 'BOOLEAN'),
    ({'module_content': SECRET}, 'content', 'STRING'),
    ({'module_content': {SECRET: SECRET}}, 'paragraphs', 'MISSING'),
    ({'module_content': {'paragraphs': SECRET}}, 'paragraphs', 'STRING'),
    ({'module_content': {'paragraphs': [SECRET]}}, 'paragraph', 'STRING'),
    ({'module_content': {'paragraphs': [{'text': None}]}}, 'text', 'NULL'),
    ({'module_content': {'paragraphs': [{'text': {}}]}}, 'nodes', 'MISSING'),
    ({'module_content': {'paragraphs': [{'text': {'nodes': [SECRET]}}]}}, 'node', 'STRING'),
    ({'module_content': {'paragraphs': [{'text': {'nodes': [{SECRET: SECRET}]}}]}}, 'node', 'OBJECT'),
    ({'module_content': {'paragraphs': [{'text': {'nodes': [{'word': None}]}}]}}, 'word', 'NULL'),
    ({'module_content': {'paragraphs': [{'text': {'nodes': [{'word': {}}]}}]}}, 'words', 'MISSING'),
    ({'module_content': {'paragraphs': [{'text': {'nodes': [{'rich': SECRET}]}}]}}, 'rich', 'STRING'),
    ({'module_content': {'paragraphs': [{'text': {'nodes': [{'rich': {'text': 3}}]}}]}}, 'rich_text', 'INTEGER'),
])
def test_fixed_field_and_type_with_no_source_text(module, field, kind):
    with pytest.raises(MetadataStructureError) as caught:
        metadata({'detail': {'modules': [{}, module]}})
    event = caught.value.diagnostic
    assert event['field'] == field and event['value_type'] == kind
    assert event['module_index'] == 1
    assert SECRET not in json.dumps(event) + str(caught.value)
    assert len(json.dumps(event)) < 400


@pytest.mark.parametrize('name,published,field,kind', [
    (None, 1, 'author_name', 'NULL'),
    (SECRET, 'invalid', 'author_published', 'STRING'),
    (SECRET, True, 'author_published', 'BOOLEAN'),
])
def test_author_field_failure_is_distinct(name, published, field, kind):
    with pytest.raises(MetadataStructureError) as caught:
        author_fields({'name': name, 'pub_ts': published})
    assert caught.value.diagnostic['field'] == field
    assert caught.value.diagnostic['value_type'] == kind
    assert SECRET not in json.dumps(caught.value.diagnostic)


def test_known_text_tags_empty_and_plain_text_unchanged():
    value = {'detail': {'modules': [
        {'module_title': {'text': 'title', 'tags': ['tag', 'tag']}},
        {'module_topic': {'name': 'topic'}},
        {'module_content': {'paragraphs': [
            {'text': {'nodes': [{'word': {'words': '<b>plain</b>'}}]}},
            {'text': {'nodes': [{'rich': {'orig_text': '@name', 'text': 'name'}}]}}
        ]}}
    ]}}
    result = metadata(value)
    assert result['description'] == '<b>plain</b>\n@name'
    assert result['tags'] == ['tag'] and result['topics'] == ['topic']
    assert result['description_status'] == result['tags_status'] == 'present'
    assert metadata({'detail': {'modules': []}})['description_status'] == 'empty'
    assert author_fields({'name': 'name', 'pub_ts': 1}) == ('name', 1)


@pytest.mark.parametrize('case,field', [('title', 'title_text'), ('tag', 'tag'),
                                     ('text', 'word'), ('author', 'author_published')])
def test_native_path_emits_diagnostic_before_budget_and_stops_at_two_requests(native, case, field, caplog, capsys):
    modules = native['article']['detail']['modules']
    if case == 'title': modules.append({'module_type': 'title', 'module_title': {'text': None}})
    elif case == 'tag': modules.append({'module_type': 'title', 'module_title': {'tags': [{SECRET: SECRET}]}})
    elif case == 'text': modules[1]['module_content']['paragraphs'].append({'text': {'nodes': [{'word': None}]}})
    else: modules[0]['module_author']['pub_ts'] = SECRET
    events = []
    g = Guard('https://www.bilibili.com/opus/1249456774436618276',
              clock=native['time'].clock, sleep=native['time'].sleep)
    with pytest.raises(Stop, match='gallery_metadata_structure_unknown'):
        run_stage('1249456774436618276', 'prepare', native['directory'], native['jar'], 'unused',
                  guard=g, emit=events.append)
    assert [e['type'] for e in events] == ['metadata_diagnostic', 'budget']
    assert events[0]['field'] == field
    assert len(native['calls']) == events[1]['network_requests'] == 2
    assert events[1]['media_requests'] == events[1]['total_media_bytes'] == 0
    assert not list(native['directory'].iterdir())
    captured = capsys.readouterr()
    assert SECRET not in json.dumps(events) + caplog.text + captured.out + captured.err


def test_worker_persists_only_safe_diagnostic_on_failure(native, tmp_path, monkeypatch):
    from bilidesk import gallery_worker
    from bilidesk.library import Library
    lib = Library(tmp_path / 'data'); lib.initialize()
    tool = tmp_path / 'ffmpeg.exe'; tool.touch(); tool.with_name('ffprobe.exe').touch()
    for key, value in dict(root=str(tmp_path / 'archive'), offline='0', gallery_online='1',
                           ffmpeg=str(tool), profile='synthetic').items(): lib.set_setting(key, value)
    monkeypatch.setattr(gallery_worker, 'load_bili_cookies', lambda *a: (native['jar'], {}))
    # Use the real engine with its fake adapter; no successful media dispatch can occur.
    modules = native['article']['detail']['modules']
    modules.append({'module_type': 'title', 'module_title': {'text': None}})
    events = []
    with pytest.raises(Stop, match='gallery_metadata_structure_unknown'):
        gallery_worker.execute({'data_dir': str(lib.directory), 'action': 'gallery_prepare',
                                'payload': {'url': 'https://www.bilibili.com/opus/1249456774436618276'}}, events.append)
    reports = list((tmp_path / 'archive').rglob('metadata-diagnostic.json'))
    assert len(reports) == 1
    persisted = json.loads(reports[0].read_text(encoding='utf-8'))
    assert persisted == events[0] and persisted['field'] == 'title_text'
    assert SECRET not in reports[0].read_text(encoding='utf-8')
    assert len(native['calls']) == 2
    with lib.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM gallery_works').fetchone()[0] == 0
