import os
from pathlib import Path
import time
import threading
import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from bilidesk.bridge import Bridge
import bilidesk.bridge as bridge_module


def test_qml_real_bridge_browse_missing_file_and_shutdown(tmp_path,monkeypatch):
    monkeypatch.setenv('PYTHONPATH',str(Path(__file__).resolve().parents[1]/'src'))
    app=QGuiApplication.instance() or QGuiApplication([])
    bridge=Bridge(tmp_path/'data')
    bridge.lib.save_work(dict(bvid='BV13x41117TL',uid='10',title='合成作品'),[dict(cid='100',part=1)])
    engine=QQmlApplicationEngine(); engine.rootContext().setContextProperty('bridge',bridge)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]/'resources/Main.qml')))
    assert engine.rootObjects()
    bridge.refresh()
    deadline=time.monotonic()+5
    while not bridge.works and time.monotonic()<deadline: app.processEvents(); time.sleep(.01)
    assert bridge.total==1
    bridge.openWork('BV13x41117TL'); engine.rootObjects()[0].setProperty('pageName','detail')
    while not bridge.detail and time.monotonic()<deadline: app.processEvents(); time.sleep(.01)
    assert len(bridge.detail['assets'])==1
    bridge.openLocal('100',False)
    while '不存在' not in bridge.message and time.monotonic()<deadline: app.processEvents(); time.sleep(.02)
    assert '不存在' in bridge.message
    assert bridge.paused  # Opening a file must not resume the download queue.
    blocked=bridge.lib.enqueue('metadata',{},'rate_limit')
    bridge.active={'id':blocked}; bridge.terminal=False; bridge._paused=False
    bridge.fail('http_412')
    assert bridge.paused and bridge.lib.setting('online_stopped')=='1'
    bridge.lib.enqueue('metadata',{},'pending_online')
    assert bridge.lib.next_task(offline_only=True) is None
    bridge.active=None
    bridge.shutdown()
    engine.rootObjects()[0].close()


def spin_until(app,condition):
    deadline=time.monotonic()+5
    while not condition() and time.monotonic()<deadline:
        app.processEvents(); time.sleep(.005)
    assert condition()


def test_local_open_bypasses_busy_download_and_coalesces_clicks(tmp_path,monkeypatch):
    app=QGuiApplication.instance() or QGuiApplication([])
    bridge=Bridge(tmp_path/'data'); bridge.timer.stop()
    bvid='BV13x41117TL'; file=tmp_path/'local.mp4'; file.write_bytes(b'synthetic')
    bridge.lib.save_work(dict(bvid=bvid,uid='10'),[dict(cid='100',part=1)])
    bridge.lib.register_file(bvid,'100',dict(path=str(file),size=9,sha256='synthetic',duration=1))
    bridge._bvid=bvid; bridge.process=object()  # Serial queue is occupied; no timer can dispatch.
    release=threading.Event(); started=threading.Event(); checked=[]; opened=[]
    real_check=bridge_module.check_local_file; main_thread=threading.get_ident()
    def check(*args):
        checked.append(threading.get_ident()); started.set(); assert release.wait(5)
        return real_check(*args)
    monkeypatch.setattr(bridge_module,'check_local_file',check)
    monkeypatch.setattr(bridge_module.QDesktopServices,'openUrl',lambda url:opened.append((url.toLocalFile(),threading.get_ident())) or True)
    try:
        bridge.openLocal('100',False); assert started.wait(2)
        bridge.openLocal('100',False); bridge._bvid='BV1bK411W797'
        release.set(); spin_until(app,lambda:bool(opened))
        assert len(opened)==1 and Path(opened[0][0])==file and opened[0][1]==main_thread
        assert len(checked)==1 and checked[0]!=main_thread
        assert bridge.lib.tasks()==[] and bridge.paused
    finally:
        release.set(); bridge.process=None; bridge.shutdown()


@pytest.mark.parametrize('condition,expected',[('changed','大小已变化'),('removed','不存在'),('shell','默认程序启动失败')])
def test_local_open_preserves_file_and_shell_errors(tmp_path,monkeypatch,condition,expected):
    app=QGuiApplication.instance() or QGuiApplication([])
    bridge=Bridge(tmp_path/'data'); bridge.timer.stop()
    file=tmp_path/'local.mp4'; file.write_bytes(b'original'); bvid='BV13x41117TL'
    bridge.lib.save_work(dict(bvid=bvid,uid='10'),[dict(cid='100',part=1)])
    bridge.lib.register_file(bvid,'100',dict(path=str(file),size=8,sha256='synthetic',duration=1))
    bridge._bvid=bvid; opened=[]
    monkeypatch.setattr(bridge_module.QDesktopServices,'openUrl',lambda url:opened.append(url) or False)
    if condition=='changed': file.write_bytes(b'different-size')
    if condition=='removed': file.unlink()
    try:
        bridge.openLocal('100',False)
        spin_until(app,lambda:expected in bridge.message)
        assert bool(opened)==(condition=='shell')
    finally: bridge.shutdown()


def test_no_player_launch_after_shutdown(tmp_path,monkeypatch):
    app=QGuiApplication.instance() or QGuiApplication([])
    bridge=Bridge(tmp_path/'data'); bridge.timer.stop(); release=threading.Event(); started=threading.Event(); opened=[]
    def check(*args):
        started.set(); release.wait(5); return {'path':str(tmp_path/'synthetic.mp4'),'folder':False}
    monkeypatch.setattr(bridge_module,'check_local_file',check)
    monkeypatch.setattr(bridge_module.QDesktopServices,'openUrl',lambda url:opened.append(url) or True)
    bridge.openLocal('100',False); assert started.wait(2)
    release.set(); bridge.shutdown(); app.processEvents()
    assert not opened


def test_failed_query_is_visible_and_can_recover_without_erasing_library(tmp_path,monkeypatch):
    import json
    app=QGuiApplication.instance() or QGuiApplication([])
    bridge=Bridge(tmp_path/'data'); bridge.timer.stop()
    bridge.lib.save_work(dict(bvid='BV13x41117TL',uid='10'),[dict(cid='100',part=1)])
    real_snapshot=bridge.lib.snapshot
    def broken(*args): raise ValueError('SENSITIVE_SENTINEL')
    monkeypatch.setattr(bridge.lib,'snapshot',broken)
    try:
        bridge.refresh(); spin_until(app,lambda:bridge.loadState=='error')
        assert '读取失败' in bridge.message
        raw=(bridge.lib.directory/'library-status.json').read_text(encoding='utf-8')
        report=json.loads(raw); assert report['state']=='error' and report['error_type']=='ValueError'
        assert 'SENSITIVE_SENTINEL' not in raw
        monkeypatch.setattr(bridge.lib,'snapshot',real_snapshot)
        bridge.refresh(); spin_until(app,lambda:bridge.loadState=='ready' and bridge.total==1)
        assert not bridge.message and bridge.authors[0]['uid']=='10'
    finally: bridge.shutdown()
