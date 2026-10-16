"""Stable identities and bounded presentation queries; never network or media IO."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

CATEGORIES = ('图文／图片', '投稿视频', '动态视频', '待分类')
ACTIVE = ('queued', 'running')


def identity(bvid, uid=None, cid=None):
    if not re.fullmatch(r'BV[0-9A-Za-z]{10}', str(bvid)):
        raise ValueError('invalid_bvid')
    if uid is not None and (not str(uid).isdigit() or int(uid) <= 0):
        raise ValueError('invalid_uid')
    if cid is not None and (not str(cid).isdigit() or int(cid) <= 0):
        raise ValueError('invalid_cid')


class Library:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.path = self.directory / 'library.sqlite3'

    @contextmanager
    def connection(self, read=False):
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro' if read else str(self.path), uri=read, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            if not read:
                db.commit()
        except BaseException:
            if not read:
                db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS authors(uid TEXT PRIMARY KEY, name TEXT NOT NULL, bio TEXT DEFAULT '', group_name TEXT DEFAULT '未分组');
            CREATE TABLE IF NOT EXISTS works(bvid TEXT PRIMARY KEY, uid TEXT NOT NULL REFERENCES authors(uid), title TEXT NOT NULL,
              description TEXT DEFAULT '', published INTEGER DEFAULT 0, category TEXT DEFAULT '待分类', category_evidence TEXT DEFAULT '',
              tags TEXT DEFAULT '[]', tags_status TEXT DEFAULT 'pending', updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS works_author ON works(uid,published DESC,bvid);
            CREATE TABLE IF NOT EXISTS assets(bvid TEXT NOT NULL REFERENCES works(bvid), cid TEXT NOT NULL, part INTEGER NOT NULL,
              title TEXT DEFAULT '', path TEXT DEFAULT '', sha256 TEXT DEFAULT '', size INTEGER DEFAULT 0, duration REAL DEFAULT 0,
              status TEXT DEFAULT 'remote', PRIMARY KEY(bvid,cid), UNIQUE(bvid,part));
            CREATE TABLE IF NOT EXISTS previews(bvid TEXT NOT NULL,cid TEXT NOT NULL, ordinal INTEGER NOT NULL,path TEXT NOT NULL,
              seconds REAL NOT NULL,sha256 TEXT NOT NULL, PRIMARY KEY(bvid,cid,ordinal), FOREIGN KEY(bvid,cid) REFERENCES assets(bvid,cid));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
              state TEXT NOT NULL, phase TEXT DEFAULT '', progress REAL DEFAULT 0, speed REAL DEFAULT 0, error TEXT DEFAULT '', created REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS live_tasks ON tasks(key) WHERE state IN ('queued','running');
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            PRAGMA user_version=1;
            ''')

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE tasks SET state='interrupted',phase='程序已退出，等待重试',error='worker_interrupted' WHERE state='running'")

    def setting(self, key, default=''):
        with self.connection(True) as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key, value):
        with self.connection() as db:
            db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,str(value)))

    def save_work(self, work, assets):
        identity(work['bvid'], work['uid'])
        if work.get('tags_status','pending') not in ('pending','present','empty','failed'):
            raise ValueError('invalid_tag_status')
        with self.connection() as db:
            prior=db.execute('SELECT uid FROM works WHERE bvid=?',(work['bvid'],)).fetchone()
            if prior and prior['uid']!=str(work['uid']):
                raise ValueError('author_identity_conflict')
            db.execute('INSERT INTO authors(uid,name) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET name=CASE WHEN excluded.name LIKE ? THEN authors.name ELSE excluded.name END',
                       (str(work['uid']),work.get('author') or '作者 UID '+str(work['uid']), '作者 UID %'))
            db.execute('''INSERT INTO works(bvid,uid,title,description,published,tags,tags_status,updated) VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(bvid) DO UPDATE SET uid=excluded.uid,title=CASE WHEN excluded.title LIKE '% · 标题待获取' THEN works.title ELSE excluded.title END,
              description=CASE WHEN excluded.description='' THEN works.description ELSE excluded.description END,
              published=CASE WHEN excluded.published>0 THEN excluded.published ELSE works.published END,
              tags=CASE WHEN excluded.tags_status='pending' THEN works.tags ELSE excluded.tags END,
              tags_status=CASE WHEN excluded.tags_status='pending' THEN works.tags_status ELSE excluded.tags_status END,updated=excluded.updated''',
              (work['bvid'],str(work['uid']),work.get('title') or work['bvid']+' · 标题待获取', work.get('description',''),
               int(work.get('published') or 0),json.dumps(work.get('tags',[]),ensure_ascii=False),work.get('tags_status','pending'),time.time()))
            for asset in assets:
                identity(work['bvid'], cid=asset['cid'])
                if int(asset['part']) < 1:
                    raise ValueError('invalid_part')
                db.execute('''INSERT INTO assets(bvid,cid,part,title,duration) VALUES(?,?,?,?,?)
                  ON CONFLICT(bvid,cid) DO UPDATE SET title=excluded.title,duration=CASE WHEN assets.status!='complete' AND excluded.duration>0 THEN excluded.duration ELSE assets.duration END''',
                  (work['bvid'],str(asset['cid']),int(asset['part']),asset.get('title',''),float(asset.get('duration') or 0)))

    def register_file(self, bvid, cid, file, previews=()):
        identity(bvid,cid=cid)
        with self.connection() as db:
            if not db.execute('SELECT 1 FROM assets WHERE bvid=? AND cid=?',(bvid,str(cid))).fetchone():
                raise ValueError('asset_missing')
            db.execute("UPDATE assets SET path=?,sha256=?,size=?,duration=?,status='complete' WHERE bvid=? AND cid=?",
                       (str(file['path']),file['sha256'],file['size'],file['duration'],bvid,str(cid)))
            for frame in previews:
                db.execute('INSERT INTO previews VALUES(?,?,?,?,?,?) ON CONFLICT(bvid,cid,ordinal) DO UPDATE SET path=excluded.path,seconds=excluded.seconds,sha256=excluded.sha256',
                           (bvid,str(cid),frame['ordinal'],str(frame['path']),frame['seconds'],frame['sha256']))

    def classify(self, bvid, category):
        if category not in CATEGORIES:
            raise ValueError('invalid_category')
        with self.connection() as db:
            db.execute("UPDATE works SET category=?,category_evidence='user_correction' WHERE bvid=?",(category,bvid))

    def group_author(self, uid, group):
        with self.connection() as db:
            db.execute('UPDATE authors SET group_name=? WHERE uid=?',(str(group).strip()[:60] or '未分组',str(uid)))

    def snapshot(self, uid='', search='', category='', undownloaded=False, offset=0, limit=40):
        offset,limit=max(0,int(offset)),max(1,min(40,int(limit)))
        clauses,params=['1=1'],[]
        if uid:
            clauses.append('w.uid=?'); params.append(str(uid))
        if category:
            clauses.append('w.category=?'); params.append(category)
        if search:
            clauses.append("(w.title LIKE ? ESCAPE '\\' OR w.description LIKE ? ESCAPE '\\' OR w.tags LIKE ? ESCAPE '\\' OR a.name LIKE ? ESCAPE '\\' OR a.uid=?)")
            pattern='%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            params += [pattern]*4+[search]
        if undownloaded:
            clauses.append("EXISTS(SELECT 1 FROM assets x WHERE x.bvid=w.bvid AND x.status!='complete')")
        where=' AND '.join(clauses)
        with self.connection(True) as db:
            authors=[dict(r) for r in db.execute('SELECT a.*,COUNT(w.bvid) count FROM authors a LEFT JOIN works w ON w.uid=a.uid GROUP BY a.uid ORDER BY a.name')]
            total=db.execute('SELECT COUNT(*) FROM works w JOIN authors a ON w.uid=a.uid WHERE '+where,params).fetchone()[0]
            rows=db.execute('''SELECT w.*,a.name author,(SELECT COUNT(*) FROM assets x WHERE x.bvid=w.bvid) parts,
              (SELECT COUNT(*) FROM assets x WHERE x.bvid=w.bvid AND x.status='complete') local_count,
              (SELECT path FROM previews p WHERE p.bvid=w.bvid ORDER BY cid,ordinal LIMIT 1) cover
              FROM works w JOIN authors a ON w.uid=a.uid WHERE '''+where+' ORDER BY w.published DESC,w.bvid LIMIT ? OFFSET ?',params+[limit,offset])
            works=[dict(r) for r in rows]
        for w in works:
            w['cover']=Path(w['cover']).as_uri() if w['cover'] else ''
        return dict(authors=authors,works=works,total=total,offset=offset)

    def detail(self,bvid):
        with self.connection(True) as db:
            row=db.execute('SELECT w.*,a.name author FROM works w JOIN authors a ON w.uid=a.uid WHERE bvid=?',(bvid,)).fetchone()
            if not row:
                return {}
            work=dict(row); work['tags']=json.loads(work['tags'])
            work['assets']=[dict(r) for r in db.execute('SELECT * FROM assets WHERE bvid=? ORDER BY part',(bvid,))]
            for asset in work['assets']:
                asset['previews']=[dict(r) for r in db.execute('SELECT * FROM previews WHERE bvid=? AND cid=? ORDER BY ordinal',(bvid,asset['cid']))]
                for p in asset['previews']:
                    p['url']=Path(p['path']).as_uri()
        return work

    def enqueue(self,kind,payload,key):
        with self.connection() as db:
            old=db.execute("SELECT id FROM tasks WHERE key=? AND state IN ('queued','running')",(key,)).fetchone()
            if old:
                return old[0]
            ident=uuid.uuid4().hex
            db.execute('INSERT INTO tasks(id,kind,key,payload,state,created) VALUES(?,?,?,?,?,?)',
                       (ident,kind,key,json.dumps(payload,ensure_ascii=False),'queued',time.time()))
            return ident

    def update_task(self,ident,**fields):
        if not fields or set(fields)-{'state','phase','progress','speed','error'}:
            raise ValueError('invalid_task_update')
        with self.connection() as db:
            db.execute('UPDATE tasks SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',list(fields.values())+[ident])

    def tasks(self):
        with self.connection(True) as db:
            return [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY created DESC LIMIT 100')]

    def next_task(self,local_only=False):
        with self.connection(True) as db:
            row=db.execute("SELECT * FROM tasks WHERE state='queued'"+(" AND kind!='download'" if local_only else '')+" ORDER BY created LIMIT 1").fetchone()
            return dict(row) if row else None
