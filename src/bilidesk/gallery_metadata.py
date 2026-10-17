def metadata(article):
    paragraphs, tags, topics, title = [], [], [], ''
    modules = article['detail']['modules']
    if not isinstance(modules, list):
        raise ValueError('metadata_structure_invalid')
    for module in modules:
        if module.get('module_blocked'):
            raise ValueError('effective_blocked_module')
        heading = module.get('module_title')
        if heading:
            title = heading.get('text', '')
            if not isinstance(title,str): raise ValueError('title_structure_unknown')
            raw = heading.get('tags', [])
            if raw is None: raw=[]
            if not isinstance(raw, list) or any(not isinstance(t, str) for t in raw):
                raise ValueError('tags_structure_unknown')
            tags.extend(raw)
        topic = module.get('module_topic')
        if topic:
            if not isinstance(topic.get('name'), str):
                raise ValueError('tags_structure_unknown')
            topics.append(topic['name'])
        content = module.get('module_content')
        if content:
            for paragraph in content['paragraphs']:
                if 'text' not in paragraph:
                    continue
                parts = []
                for node in paragraph['text']['nodes']:
                    if 'word' in node:
                        value = node['word']['words']
                    elif 'rich' in node:
                        value = node['rich'].get('orig_text') or node['rich']['text']
                    else:
                        raise ValueError('text_structure_unknown')
                    if not isinstance(value, str):
                        raise ValueError('text_structure_unknown')
                    parts.append(value)
                paragraphs.append(''.join(parts))
    description = '\n'.join(paragraphs)
    return dict(title=title, description=description,
                description_status='present' if description else 'empty',
                tags=list(dict.fromkeys(tags)), topics=list(dict.fromkeys(topics)),
                tags_status='present' if tags or topics else 'empty')
