"""Fixed-page, two-request metadata comparison. Never creates a download job."""
import copy
import hashlib
import json
from importlib.metadata import version

from .gallery_engine import PINS, descriptor, fingerprint
from .gallery_metadata import metadata, author_fields, MetadataStructureError
from .gallery_transport import Guard, Stop, NAV, audit_state, original_modules, native_configuration, typed

IDENT = '1249456774436618276'
UID = '1337856191'
PAGE = 'https://www.bilibili.com/opus/' + IDENT


class PageGuard(Guard):
    def __init__(self, **kwargs):
        super().__init__(PAGE, **kwargs)
        self.deadline = self.started + 60
        self.sample.requests = 2
        self.sample.assets = 0

    def kind(self, request):
        if self.count >= 2:
            self.stop('request_budget_exhausted')
        kind = super().kind(request)
        if kind == 'media':
            self.stop('diagnostic_media_forbidden')
        return kind


def structural_snapshot(article):
    """Only known field names, types and positions; never traverse arbitrary keys."""
    fields = []

    def add(field, value, position, present=True):
        if len(fields) >= 4096:
            raise Stop('metadata_diagnostic_limit')
        fields.append(dict(field=field, **position, **typed(value, present)))

    def child(parent, key, label, position):
        exists = isinstance(parent, dict) and key in parent
        value = parent.get(key) if isinstance(parent, dict) else None
        add(label, value, position, exists)
        return value

    for mi, module in enumerate(article['detail']['modules']):
        pos = dict(module_index=mi)
        for key, label in [('module_author', 'author'), ('module_title', 'title'),
                           ('module_topic', 'topic'), ('module_content', 'content')]:
            value = child(module, key, label, pos)
            if not isinstance(value, dict):
                continue
            if label == 'author':
                child(value, 'name', 'author_name', pos)
                child(value, 'pub_ts', 'author_published', pos)
            elif label == 'title':
                child(value, 'text', 'title_text', pos)
                tags = child(value, 'tags', 'tags', pos)
                if isinstance(tags, list):
                    for ti, tag in enumerate(tags): add('tag', tag, pos | dict(tag_index=ti))
            elif label == 'topic':
                child(value, 'name', 'topic_name', pos)
            else:
                paragraphs = child(value, 'paragraphs', 'paragraphs', pos)
                if not isinstance(paragraphs, list): continue
                for pi, paragraph in enumerate(paragraphs):
                    pp = pos | dict(paragraph_index=pi)
                    add('paragraph', paragraph, pp)
                    child(paragraph, 'pic', 'picture', pp)
                    text = child(paragraph, 'text', 'text', pp)
                    nodes = child(text, 'nodes', 'nodes', pp)
                    if not isinstance(nodes, list): continue
                    for ni, node in enumerate(nodes):
                        np = pp | dict(node_index=ni)
                        add('node', node, np)
                        word = child(node, 'word', 'word', np)
                        rich = child(node, 'rich', 'rich', np)
                        if isinstance(word, dict): child(word, 'words', 'words', np)
                        if isinstance(rich, dict):
                            child(rich, 'orig_text', 'rich_original', np)
                            child(rich, 'text', 'rich_text', np)
    return fields


def text_summary(value):
    return dict(**typed(value), characters=len(value) if isinstance(value, str) else None,
                sha256=hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None)


def sequence_summary(values):
    valid = isinstance(values, list) and all(isinstance(v, str) for v in values)
    return dict(**typed(values), count=len(values) if isinstance(values, list) else None,
                sha256=fingerprint(values) if valid else None,
                characters=sum(map(len, values)) if valid else None)


def inspect(directory, jar, baseline, *, guard=None):
    """Both parsers consume copies of one fetched state, even on adapter rejection."""
    from gallery_dl.extractor.bilibili import BilibiliArticleExtractor
    from gallery_dl.extractor.common import Message
    guard = guard or PageGuard()
    result = dict(schema=1, diagnostic='NOT_COMPLETED', adapter={'status':'NOT_RUN'},
                  native={'status':'NOT_RUN'}, baseline='NOT_CHECKED')
    extractor = None
    try:
        if type(guard) is not PageGuard: raise Stop('diagnostic_guard_required')
        if any(version(k) != v for k, v in PINS.items()): raise Stop('gallery_dependency_changed')
        with native_configuration(directory):
            extractor = BilibiliArticleExtractor.from_url(PAGE)
            extractor.initialize()
            extractor.session.cookies.update(jar)
            guard.install(extractor.session)
            nav = extractor.request(NAV).json()
            account = nav.get('data')
            if (nav.get('code') != 0 or not isinstance(account, dict) or account.get('isLogin') is not True
                    or type(account.get('mid')) is not int or account['mid'] <= 0):
                raise Stop('login_expired')
            if any(c.value != str(account['mid']) for c in jar if c.name == 'DedeUserID'):
                raise Stop('account_cookie_identity_mismatch')
            guard.account_ok = True
            original_request = extractor.request

            def request(url, *args, **kwargs):
                if url != PAGE: raise Stop('unexpected_source_endpoint')
                response = original_request(url, *args, **kwargs)
                audit = audit_state(response.content.decode('utf-8', errors='strict'))
                if audit['status'] != 'PARSED' or audit['assignment_count'] != 1 or audit['duplicate_member_count']:
                    raise Stop('page_state_ambiguous_or_invalid')
                return response

            extractor.request = request
            article = extractor.api.article(IDENT)  # Actual pinned upstream initial-state parser.
            structure = original_modules(article)
            result['modules'] = structure
            if str(article.get('id')) != IDENT: raise Stop('work_identity_mismatch')
            if structure['status'] != 'OBSERVED': raise Stop('original_modules_invalid')
            if any(f['truthy'] or f['value_type'] not in ('NULL','OBJECT') for f in structure['blocked_fields']):
                raise Stop('effective_blocked_module')
            authors = [m['module_author'] for m in article['detail']['modules'] if m.get('module_author')]
            if not authors or any(not isinstance(a, dict) or str(a.get('mid')) != UID for a in authors):
                raise Stop('author_identity_mismatch')
            result['fields'] = structural_snapshot(article)
            fragments = []
            try:
                converted = metadata(copy.deepcopy(article), fragments=fragments)
                author_fields(authors[0])
                result['adapter'] = dict(status='PASS', description=text_summary(converted['description']),
                    fragments=sequence_summary(fragments), title=text_summary(converted['title']),
                    tags=sequence_summary(converted['tags']), topics=sequence_summary(converted['topics']))
            except MetadataStructureError as exc:
                result['adapter'] = dict(status='REJECTED', diagnostic=exc.diagnostic)
            # No request is allowed during this in-memory extraction; errors never authorize media.
            def forbidden(*args, **kwargs): raise Stop('diagnostic_extra_request_forbidden')
            extractor.request = forbidden
            extractor.api.article = lambda ident: copy.deepcopy(article) if ident == IDENT else forbidden()
            rows = []
            directory_seen = False
            for kind, url, data in extractor.items():
                if str(data.get('id')) != IDENT or str(data.get('user_id')) != UID:
                    raise Stop('message_identity_mismatch')
                if kind == Message.Directory and not directory_seen:
                    directory_seen = True
                    if type(data.get('count')) is not int or data['count'] != 4: raise Stop('image_count_mismatch')
                    result['native'] = dict(status='PASS', title=text_summary(data.get('title')),
                        fragments=sequence_summary(data.get('content')), tags=sequence_summary(data.get('tags')),
                        topic=text_summary(data.get('topic')))
                elif kind == Message.Url and directory_seen:
                    row = descriptor(url, data)
                    rows.append(dict(sequence=row['sequence'], suffix='l' if row['role']=='live_video' else '',
                                     format=row['format'], source_path_sha256=row['source_path_sha256']))
                    if len(rows) > 4: raise Stop('image_count_mismatch')
                else: raise Stop('message_contract_changed')
            if len(rows) != 4 or rows != baseline['assets'] or baseline['author_uid'] != UID:
                raise Stop('fixed_sample_baseline_mismatch')
            result['baseline'] = 'PASS'
            result['manifest_sha256'] = fingerprint(rows)
            result['text_fragments_equal'] = (result['adapter']['fragments'] == result['native']['fragments']
                                              if result['adapter']['status']=='PASS' else None)
            result['diagnostic'] = 'COMPLETED'
    except Stop as exc:
        result['stop_reason'] = exc.reason
    except BaseException:
        result['stop_reason'] = 'metadata_inspection_failed'
    finally:
        result['budget'] = guard.summary()
        guard.close()
        if extractor: extractor.session.close()
    return result
