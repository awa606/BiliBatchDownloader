"""Strict metadata conversion; structural errors contain no source text."""

_MISSING = object()
_REASONS = {'metadata_structure_invalid', 'effective_blocked_module',
            'title_structure_unknown', 'tags_structure_unknown',
            'text_structure_unknown', 'author_structure_unknown'}
_FIELDS = {'article', 'detail', 'modules', 'module', 'blocked', 'title', 'title_text',
           'tags', 'tag', 'topic', 'topic_name', 'content', 'paragraphs', 'paragraph',
           'text', 'nodes', 'node', 'word', 'words', 'rich', 'rich_text',
           'author_name', 'author_published'}


class MetadataStructureError(ValueError):
    def __init__(self, reason, field, value, **position):
        assert reason in _REASONS and field in _FIELDS
        assert all(k in ('module_index', 'paragraph_index', 'node_index', 'tag_index')
                   and type(v) is int and v >= 0 for k, v in position.items())
        kind = ('MISSING' if value is _MISSING else 'NULL' if value is None else
                {dict: 'OBJECT', list: 'ARRAY', str: 'STRING', bool: 'BOOLEAN',
                 int: 'INTEGER', float: 'NUMBER'}.get(type(value), 'OTHER'))
        self.diagnostic = dict(type='metadata_diagnostic', schema=1, status='REJECTED',
                               reason=reason, field=field, value_type=kind, **position)
        super().__init__(reason)


def _require(value, expected, reason, field, position):
    if type(value) is not expected:
        raise MetadataStructureError(reason, field, value, **position)
    return value


def author_fields(author):
    name = _require(author.get('name', _MISSING), str, 'author_structure_unknown', 'author_name', {})
    published = _require(author.get('pub_ts', _MISSING), int, 'author_structure_unknown', 'author_published', {})
    return name, published


def metadata(article):
    paragraphs, tags, topics, title = [], [], [], ''
    reason = 'metadata_structure_invalid'
    _require(article, dict, reason, 'article', {})
    detail = _require(article.get('detail', _MISSING), dict, reason, 'detail', {})
    modules = _require(detail.get('modules', _MISSING), list, reason, 'modules', {})
    for index, module in enumerate(modules):
        position = dict(module_index=index)
        _require(module, dict, reason, 'module', position)
        if module.get('module_blocked'):
            raise MetadataStructureError('effective_blocked_module', 'blocked', module['module_blocked'], **position)
        heading = module.get('module_title')
        if heading:
            _require(heading, dict, 'title_structure_unknown', 'title', position)
            title = _require(heading.get('text', ''), str, 'title_structure_unknown', 'title_text', position)
            raw = heading.get('tags', [])
            if raw is None: raw=[]
            _require(raw, list, 'tags_structure_unknown', 'tags', position)
            for tag_index, tag in enumerate(raw):
                _require(tag, str, 'tags_structure_unknown', 'tag', position | dict(tag_index=tag_index))
            tags.extend(raw)
        topic = module.get('module_topic')
        if topic:
            _require(topic, dict, 'tags_structure_unknown', 'topic', position)
            topics.append(_require(topic.get('name', _MISSING), str, 'tags_structure_unknown', 'topic_name', position))
        content = module.get('module_content')
        if content:
            _require(content, dict, 'text_structure_unknown', 'content', position)
            content_paragraphs = _require(content.get('paragraphs', _MISSING), list,
                                          'text_structure_unknown', 'paragraphs', position)
            for paragraph_index, paragraph in enumerate(content_paragraphs):
                paragraph_position = position | dict(paragraph_index=paragraph_index)
                _require(paragraph, dict, 'text_structure_unknown', 'paragraph', paragraph_position)
                if 'text' not in paragraph:
                    continue
                text = _require(paragraph['text'], dict, 'text_structure_unknown', 'text', paragraph_position)
                nodes = _require(text.get('nodes', _MISSING), list, 'text_structure_unknown', 'nodes', paragraph_position)
                parts = []
                for node_index, node in enumerate(nodes):
                    node_position = paragraph_position | dict(node_index=node_index)
                    _require(node, dict, 'text_structure_unknown', 'node', node_position)
                    if 'word' in node:
                        word = _require(node['word'], dict, 'text_structure_unknown', 'word', node_position)
                        value = _require(word.get('words', _MISSING), str, 'text_structure_unknown', 'words', node_position)
                    elif 'rich' in node:
                        rich = _require(node['rich'], dict, 'text_structure_unknown', 'rich', node_position)
                        value = _require(rich.get('orig_text') or rich.get('text', _MISSING), str,
                                         'text_structure_unknown', 'rich_text', node_position)
                    else:
                        raise MetadataStructureError('text_structure_unknown', 'node', node, **node_position)
                    parts.append(value)
                paragraphs.append(''.join(parts))
    description = '\n'.join(paragraphs)
    return dict(title=title, description=description,
                description_status='present' if description else 'empty',
                tags=list(dict.fromkeys(tags)), topics=list(dict.fromkeys(topics)),
                tags_status='present' if tags or topics else 'empty')
