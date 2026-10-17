import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'experiments'))
from gallery_selection_spike import metadata, selected_messages
from test_gallery_native_probe import native, SECRET
from gallery_native_probe import native_configuration


def test_native_two_phases_keep_only_selected_live_video(native, tmp_path):
    from gallery_dl import job
    from gallery_dl.extractor.bilibili import BilibiliArticleExtractor
    pics = native['article']['detail']['modules'][1]['module_content']['paragraphs'][0]['pic']['pics']
    for i, pic in enumerate(pics, 1):
        pic['live_url'] = f'https://i0.hdslb.com/bfs/{i}.mp4'
        native['media'][pic['live_url']] = b'synthetic-mp4-for-transport-only'
    for phase in ('prepare', 'download'):
        directory = tmp_path / phase
        directory.mkdir()
        with native_configuration(directory):
            extractor = BilibiliArticleExtractor.from_url('https://www.bilibili.com/opus/1249456774436618276')
            extractor.initialize()
            messages = list(selected_messages(extractor, phase, {1, 3}))
            extractor.items = lambda: iter(messages)
            task = job.DownloadJob(extractor)
            task.run()
            assert task.status == 0
            extractor.session.close()
    requests = [c['url'] for c in native['calls']]
    assert requests == ['https://www.bilibili.com/opus/1249456774436618276', *native['urls'],
                        'https://www.bilibili.com/opus/1249456774436618276',
                        pics[0]['live_url'], pics[2]['live_url']]
    assert sorted(p.name for p in (tmp_path / 'download').iterdir()) == ['01l.mp4', '03l.mp4']


def test_metadata_paragraphs_tags_topic_and_plain_text(native):
    article = copy.deepcopy(native['article'])
    article['detail']['modules'] += [
        {'module_title': {'text': '标题', 'tags': ['原站标签', '原站标签']}},
        {'module_topic': {'name': '原站话题'}},
        {'module_content': {'paragraphs': [
            {'text': {'nodes': [{'word': {'words': '第一段'}}]}},
            {'text': {'nodes': [{'rich': {'orig_text': '<b>@名字</b>', 'text': '名字'}}]}}
        ]}}]
    result = metadata(article)
    assert result == dict(title='标题', description='第一段\n<b>@名字</b>', description_status='present',
                          tags=['原站标签'], topics=['原站话题'], tags_status='present')


def test_confirmed_empty_does_not_invent_tags(native):
    result = metadata(native['article'])
    assert result['description_status'] == result['tags_status'] == 'empty'


@pytest.mark.parametrize('value', [[{'badge': 'system'}], 'tag', [1]])
def test_unknown_tag_shapes_stop(native, value):
    native['article']['detail']['modules'].append({'module_title': {'tags': value}})
    with pytest.raises(ValueError, match='tags_structure_unknown'):
        metadata(native['article'])
