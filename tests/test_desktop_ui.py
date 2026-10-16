import os
from pathlib import Path
import time
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from bilidesk.bridge import Bridge


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
    while not bridge.message and time.monotonic()<deadline: app.processEvents(); time.sleep(.02)
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
