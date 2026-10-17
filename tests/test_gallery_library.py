import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from PySide6.QtGui import QImage, QColor, QGuiApplication

from bilidesk.library import Library
from bilidesk.gallery import scan_report,import_report,check_gallery_file,load_report
from bilidesk.media import digest


def make_report(root,live=False,count=2):
    root.mkdir(parents=True); media=root/'media'; media.mkdir()
    rows=[]
    for n in range(1,count+1):
        for video in ((False,True) if live else (False,)):
            suffix='l' if video else ''; fmt='mp4' if video else 'jpg' if live else 'png'
            p=media/f'{n:02d}{suffix}.{fmt}'
            if video: p.write_bytes(b'synthetic bytes only; actual MP4 tests elsewhere'+bytes([n]))
            else:
                image=QImage(1400,700,QImage.Format_RGB32); image.fill(QColor(n*50,90,150)); assert image.save(str(p))
            row=dict(sequence=n,suffix=suffix,format=fmt,filename=p.name,source_path_sha256=f'{n*2+int(video):064x}',
                size=p.stat().st_size,sha256=digest(p),download='PASS',decode='PASS',decoded_frames=4 if video else 1,
                duration_seconds=1 if video else None)
            if live: row['pair_role']='live_video' if video else 'image'
            rows.append(row)
    report=dict(schema_version=2 if live else 1,status='PASS',account='PASS',baseline_match='PASS',fingerprints_unchanged=True,
                sample_dynamic_id='1257912749013336118' if live else '1249456774436618276',author_uid='1337856191',declared_images=count,assets=rows)
    if live:
        report.update(sample_kind='livephoto',pairs=[dict(sequence=n,status='PASS',image_filename=f'{n:02d}.jpg',video_filename=f'{n:02d}l.mp4') for n in range(1,count+1)])
    path=root/'report.json'; path.write_text(json.dumps(report),encoding='utf-8')
    return path,report


@pytest.fixture
def library(tmp_path):
    lib=Library(tmp_path/'library'); lib.initialize(); return lib


def test_v1_migration_idempotent_preserves_video_and_rejects_future(library):
    library.save_work(dict(bvid='BV13x41117TL',uid='1'),[dict(cid='2',part=1)])
    with library.connection() as db:
        db.execute('DROP TABLE gallery_assets'); db.execute('DROP TABLE gallery_works'); db.execute('PRAGMA user_version=1')
    library.initialize(); library.initialize()
    assert library.detail('BV13x41117TL')['assets'][0]['cid']=='2'
    with library.connection() as db: db.execute('PRAGMA user_version=99')
    with pytest.raises(ValueError,match='library_version_newer'): library.initialize()
    with library.connection(True) as db: assert db.execute('PRAGMA user_version').fetchone()[0]==99


@pytest.mark.parametrize('live',[False,True])
def test_import_group_identity_search_and_repeat(library,tmp_path,live):
    path,r=make_report(tmp_path/'report',live)
    scan=scan_report(library,path); assert scan['assets']==(4 if live else 2)
    assert library.snapshot()['total']==0
    assert not import_report(library,path,scan['report_sha256'])['duplicate']
    assert import_report(library,path,scan['report_sha256'])['duplicate']
    snapshot=library.snapshot(category='图文／图片'); assert snapshot['total']==1
    assert snapshot['authors'][0]['count']==1 and snapshot['works'][0]['kind']=='gallery'
    work=library.detail(snapshot['works'][0]['work_key']); assert len(work['groups'])==2
    assert not library.snapshot(undownloaded=True)['works']
    assert library.snapshot(search=r['author_uid'])['total']==1
    assert '待获取' in work['title'] and work['tags_status']=='pending'
    for g in work['groups']:
        assert QImage(g['image']['preview_path']).width()<=1024
        assert bool(g['live_video'])==live
        assert Path(check_gallery_file(library,work['work_key'],g['sequence'],'image')['path']).is_file()
    assert not library.detail('BV1257912749013336118')


@pytest.mark.parametrize('change', ['order','missing','pair','author','source_duplicate','filename','decode','single_frame','format'])
def test_bad_reports_never_partially_commit(library,tmp_path,change):
    path,r=make_report(tmp_path/'report',True)
    if change=='order': r['assets'].reverse()
    if change=='missing': r['assets'].pop()
    if change=='pair': r['pairs'][0]['video_filename']='02l.mp4'
    if change=='author': r['author_uid']='bad-private-sentinel'
    if change=='source_duplicate': r['assets'][1]['source_path_sha256']=r['assets'][0]['source_path_sha256']
    if change=='filename': r['assets'][0]['filename']='../outside.jpg'
    if change=='decode': r['assets'][-1]['decode']='FAIL'
    if change=='single_frame': r['assets'][1]['decoded_frames']=1
    if change=='format': r['assets'][1]['format']='png'
    path.write_text(json.dumps(r))
    with pytest.raises(ValueError): import_report(library,path,digest(path))
    assert library.snapshot()['authors']==[]
    assert library.snapshot()['total']==0


@pytest.mark.parametrize('change',['report','missing','same_size','identity'])
def test_change_after_scan_rejected(library,tmp_path,change):
    path,r=make_report(tmp_path/'report'); scan=scan_report(library,path)
    if change=='report': path.write_text(path.read_text()+' ')
    elif change=='missing': (path.parent/'media/02.png').unlink()
    elif change=='same_size':
        p=path.parent/'media/02.png'; raw=p.read_bytes(); p.write_bytes(b'X'+raw[1:])
    else:
        import_report(library,path,scan['report_sha256'])
        with library.connection() as db:
            db.execute("INSERT INTO authors(uid,name) VALUES('12','previous')")
            db.execute("UPDATE gallery_works SET uid='12'")
    with pytest.raises(ValueError): import_report(library,path,scan['report_sha256'])
    assert library.snapshot()['total']==(1 if change=='identity' else 0)


def test_import_rolls_back_after_insert_failure(library,tmp_path):
    path,r=make_report(tmp_path/'report')
    with library.connection() as db:
        db.execute("CREATE TRIGGER stop_second BEFORE INSERT ON gallery_assets WHEN NEW.sequence=2 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError): import_report(library,path,digest(path))
    assert library.snapshot()['authors']==[] and library.snapshot()['total']==0


def test_registered_file_changed_missing_or_link_is_not_opened(library,tmp_path,monkeypatch):
    path,r=make_report(tmp_path/'report'); import_report(library,path,digest(path))
    key='gallery:'+r['sample_dynamic_id']; p=path.parent/'media/01.png'
    raw=p.read_bytes(); p.write_bytes(b'X'+raw[1:])
    with pytest.raises(ValueError,match='local_file_changed'): check_gallery_file(library,key,1,'image')
    p.unlink()
    with pytest.raises(ValueError,match='local_file_missing'): check_gallery_file(library,key,1,'image')
    monkeypatch.setattr(Path,'is_symlink',lambda self: self.name=='02.png')
    with pytest.raises(ValueError,match='gallery_path_invalid'): check_gallery_file(library,key,2,'image')


def test_offline_queue_and_stale_preview(tmp_path,monkeypatch):
    from bilidesk.bridge import Bridge
    from bilidesk.worker import execute
    import bilidesk.bridge as module
    app=QGuiApplication.instance() or QGuiApplication([])
    bridge=Bridge(tmp_path/'offline',offline=True); bridge.timer.stop()
    bridge.lib.enqueue('metadata',{},'inherited'); bridge.togglePause(); bridge.tick()
    assert bridge.lib.tasks()[0]['state']=='queued' and bridge.process is None and not bridge.online
    with pytest.raises(ValueError,match='offline_mode'):
        execute(dict(data_dir=str(bridge.lib.directory),action='metadata',payload={}))
    started=threading.Event(); release=threading.Event(); emitted=[]
    def check(*args):
        started.set(); release.wait(3); return dict(path=str(tmp_path/'old.mp4'),folder=False)
    monkeypatch.setattr(module,'check_gallery_file',check)
    bridge.previewReady.connect(emitted.append)
    try:
        bridge.prepareGallery(1); assert started.wait(2); bridge.invalidatePreview(); release.set()
        for _ in range(20): app.processEvents(); time.sleep(.01)
        assert not emitted
    finally: release.set(); bridge.shutdown()


def test_gallery_qml_load_and_external_verified_lane(tmp_path,monkeypatch):
    from PySide6.QtCore import QObject,QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from bilidesk.bridge import Bridge
    import bilidesk.bridge as module
    app=QGuiApplication.instance() or QGuiApplication([])
    bridge=Bridge(tmp_path/'ui',offline=True); bridge.timer.stop()
    path,r=make_report(tmp_path/'report',True); import_report(bridge.lib,path,digest(path))
    engine=QQmlApplicationEngine(); engine.rootContext().setContextProperty('bridge',bridge)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).parents[1]/'resources/Main.qml')))
    window=engine.rootObjects()[0]; bridge.openWork('gallery:'+r['sample_dynamic_id']); window.setProperty('pageName','detail')
    deadline=time.monotonic()+5
    while not window.findChild(QObject,'galleryDetail') and time.monotonic()<deadline: app.processEvents(); time.sleep(.01)
    assert window.findChild(QObject,'galleryDetail')
    opened=[]; monkeypatch.setattr(module.QDesktopServices,'openUrl',lambda url:opened.append(url.toLocalFile()) or True)
    bridge.openGallery(1,'image',False)
    while not opened and time.monotonic()<deadline: app.processEvents(); time.sleep(.01)
    assert Path(opened[0])==path.parent/'media/01.jpg'
    window.setProperty('pageName','library'); app.processEvents()
    from PySide6.QtCore import QCoreApplication,QEvent
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
    assert not window.findChild(QObject,'galleryDetail')
    window.close(); bridge.shutdown()
