"""Strict metadata conversion; structural errors contain no source text."""
import re

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
    from gallery_dl import dt
    name = _require(author.get('name', _MISSING), str, 'author_structure_unknown', 'author_name', {})
    raw = author.get('pub_ts', _MISSING)
    # The pinned extractor uses dt.parse_ts, accepting integer strings too.
    # Restrict the input grammar first: no bool, float, whitespace or unit guessing.
    if not (type(raw) is int or type(raw) is str and re.fullmatch(r'[0-9]{1,12}', raw)):
        raise MetadataStructureError('author_structure_unknown', 'author_published', raw)
    parsed = dt.parse_ts(raw)
    if not parsed or int(raw) < 0:
        raise MetadataStructureError('author_structure_unknown', 'author_published', raw)
    published = int(raw)
    return name, published


def metadata(article, *, fragments=None):
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
                # gallery-dl skips nullable text on this paragraph and continues
                # with later paragraphs. Do not swallow malformed non-null text.
                if 'text' not in paragraph or paragraph['text'] is None:
                    continue
                text = _require(paragraph['text'], dict, 'text_structure_unknown', 'text', paragraph_position)
                nodes = _require(text.get('nodes', _MISSING), list, 'text_structure_unknown', 'nodes', paragraph_position)
                parts = []
                for node_index, node in enumerate(nodes):
                    node_position = paragraph_position | dict(node_index=node_index)
                    _require(node, dict, 'text_structure_unknown', 'node', node_position)
                    node_parts = []
                    if 'word' in node and node['word'] is not None:
                        word = _require(node['word'], dict, 'text_structure_unknown', 'word', node_position)
                        node_parts.append(_require(word.get('words', _MISSING), str,
                                                   'text_structure_unknown', 'words', node_position))
                    if 'rich' in node and node['rich'] is not None:
                        rich = _require(node['rich'], dict, 'text_structure_unknown', 'rich', node_position)
                        node_parts.append(_require(rich.get('orig_text') or rich.get('text', _MISSING), str,
                                                   'text_structure_unknown', 'rich_text', node_position))
                    if not node_parts:
                        field = 'word' if 'word' in node else 'rich' if 'rich' in node else 'node'
                        raise MetadataStructureError('text_structure_unknown', field,
                                                     node if field=='node' else node[field], **node_position)
                    parts.extend(node_parts)
                paragraphs.append(''.join(parts))
                if fragments is not None:
                    fragments.extend(parts)
    description = '\n'.join(paragraphs)
    return dict(title=title, description=description,
                description_status='present' if description else 'empty',
                tags=list(dict.fromkeys(tags)), topics=list(dict.fromkeys(topics)),
                tags_status='present' if tags or topics else 'empty')
